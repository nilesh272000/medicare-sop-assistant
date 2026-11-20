"""Command line: python -m app.cli {ingest,ask,status}"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import Settings
from app.container import build_container
from app.domain.errors import DomainError
from app.ingest.indexer import IngestOptions, run_ingestion
from app.observability import configure_logging, correlation_id_var, new_correlation_id


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sop-assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="index a full library snapshot folder")
    pi.add_argument("snapshot_dir")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--allow-downgrade", action="store_true")
    pi.add_argument("--force", action="store_true", help="allow withdrawing more than half the library")
    pa = sub.add_parser("ask")
    pa.add_argument("user_id")
    pa.add_argument("question")
    sub.add_parser("status")
    args = p.parse_args(argv)

    s = Settings.from_env()
    configure_logging(s.log_level)
    c = build_container(s)
    try:
        if args.cmd == "ingest":
            rep = run_ingestion(args.snapshot_dir, c.db, c.embedder, c.policy,
                                IngestOptions(dry_run=args.dry_run, allow_downgrade=args.allow_downgrade, force=args.force))
            print(json.dumps(rep, indent=2))
        elif args.cmd == "ask":
            cid = new_correlation_id("cli")
            correlation_id_var.set(cid)
            ans, trace = c.answers.ask(args.user_id, args.question, correlation_id=cid)
            print(f"[{ans.status.value}]{' (conflict)' if ans.conflict else ''}{' (cached)' if ans.cached else ''}")
            print(ans.answer)
            for ci in ans.citations:
                print(" -", ci.label())
        elif args.cmd == "status":
            docs = c.db.active_documents()
            print(json.dumps({"generation": c.db.generation(),
                              "documents": {k: f"v{r['version']} [{r['access_level']}] {r['file_name']}" for k, r in docs.items()}},
                             indent=2))
    except DomainError as e:
        print(json.dumps({"error": {"code": e.code, "message": e.message, "details": e.details}}, indent=2), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Incremental, idempotent re-indexing of a full library snapshot.

Identity: a document is identified by the SOP id printed in its header, never by
its file name (files get renamed) or by its file bytes (a DMS re-export changes the
bytes but not the text). "Changed" means the sha256 of the normalised extracted
text differs (ADR-1).

A run:
1. parses every PDF in the snapshot (any parse failure aborts the run: the snapshot
   is the source of truth, and publishing it without one document would silently
   withdraw that document);
2. diffs it against the published library by SOP id -> added / updated / withdrawn /
   unchanged / renamed;
3. embeds only chunk texts whose embedding is not already stored (content-addressed);
4. publishes the whole diff in ONE SQLite transaction and bumps the generation.

Running it again on the same snapshot finds no differences: no embedding calls, no
publish, no generation bump, the answer cache survives.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.domain.access import AccessPolicy
from app.domain.errors import IngestionError
from app.domain.models import Chunk, SopDocument
from app.ingest.pdf_extract import extract_pdf
from app.ingest.sop_parser import parse_sop
from app.observability import correlation_id_var, log
from app.ports import Embedder
from app.store.db import Database

logger = logging.getLogger("ingest")
EMBED_BATCH = 64
MAX_WITHDRAW_FRACTION = 0.5     # guard against a half-copied snapshot folder


def embed_key(model_id: str, text: str) -> str:
    return hashlib.sha256(f"{model_id}\x00{text}".encode("utf-8")).hexdigest()


def _vkey(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in v.split("."))


@dataclass
class IngestOptions:
    allow_downgrade: bool = False
    force: bool = False           # bypass the mass-withdrawal guard
    dry_run: bool = False


def _parse_snapshot(snapshot: Path, policy: AccessPolicy, warnings: list[str]) -> dict[str, tuple[SopDocument, list[Chunk]]]:
    files = sorted(p for p in snapshot.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    by_id: dict[str, tuple[SopDocument, list[Chunk]]] = {}
    errors: list[str] = []
    for f in files:
        try:
            doc, chunks = parse_sop(extract_pdf(str(f)), f.name, policy)
        except IngestionError as e:
            errors.append(e.message)
            continue
        except Exception as e:  # corrupt PDF etc.
            errors.append(f"{f.name}: cannot read PDF ({type(e).__name__}: {e})")
            continue
        prev = by_id.get(doc.sop_id)
        if prev is None:
            by_id[doc.sop_id] = (doc, chunks)
            continue
        pdoc = prev[0]
        if pdoc.content_hash == doc.content_hash:
            warnings.append(f"{doc.sop_id}: duplicate copies {pdoc.file_name} and {doc.file_name}; identical, kept one")
        elif _vkey(doc.version) == _vkey(pdoc.version):
            errors.append(f"{doc.sop_id}: {pdoc.file_name} and {doc.file_name} both claim v{doc.version} with different content")
        else:
            newer = (doc, chunks) if _vkey(doc.version) > _vkey(pdoc.version) else prev
            older = pdoc if newer[0] is doc else doc
            warnings.append(f"{doc.sop_id}: snapshot holds v{older.version} ({older.file_name}) and "
                            f"v{newer[0].version} ({newer[0].file_name}); using v{newer[0].version}")
            by_id[doc.sop_id] = newer
    if errors:
        raise IngestionError("snapshot rejected; nothing was published", {"errors": errors})
    return by_id


def run_ingestion(
    snapshot_dir: str,
    db: Database,
    embedder: Embedder,
    policy: AccessPolicy,
    options: IngestOptions | None = None,
    *,
    _fail_after_deletes: bool = False,
) -> dict:
    opts = options or IngestOptions()
    run_id = f"ingest-{uuid.uuid4().hex[:12]}"
    token = correlation_id_var.set(run_id)
    started = time.time()
    snapshot = Path(snapshot_dir)
    report: dict = {"run_id": run_id, "snapshot": str(snapshot), "added": [], "updated": [], "withdrawn": [],
                    "renamed": [], "unchanged": [], "warnings": [], "embedding_calls": 0,
                    "texts_embedded": 0, "embeddings_reused": 0, "published": False}
    try:
        if not snapshot.is_dir():
            raise IngestionError(f"snapshot folder not found: {snapshot}")
        base_gen = db.generation()
        active = db.active_documents()
        incoming = _parse_snapshot(snapshot, policy, report["warnings"])
        if not incoming:
            raise IngestionError("snapshot contains no SOP PDFs; refusing to withdraw the whole library")

        with db.connect() as c:
            stored_model = c.execute("SELECT embed_model FROM index_state WHERE id=1").fetchone()[0]
        model_changed = bool(active) and stored_model != embedder.model_id
        if model_changed:
            report["warnings"].append(f"embedding model changed {stored_model} -> {embedder.model_id}; re-embedding all")

        upserts: list[tuple[SopDocument, list[Chunk]]] = []
        events: list[tuple[str, str, str, str]] = []
        for sop_id, (doc, chunks) in sorted(incoming.items()):
            old = active.get(sop_id)
            if old is None:
                report["added"].append(f"{sop_id} v{doc.version} ({doc.file_name})")
                upserts.append((doc, chunks))
                events.append((sop_id, doc.version, doc.content_hash, "added"))
                continue
            if _vkey(doc.version) < _vkey(old["version"]) and not opts.allow_downgrade:
                raise IngestionError(
                    f"{sop_id}: snapshot has v{doc.version} but v{old['version']} is published "
                    "(downgrade); rerun with --allow-downgrade if intended")
            if old["content_hash"] != doc.content_hash:
                if old["version"] == doc.version:
                    report["warnings"].append(f"{sop_id}: content changed without a version bump (v{doc.version})")
                note = f" - {doc.change_note}" if doc.change_note else ""
                report["updated"].append(f"{sop_id} v{old['version']} -> v{doc.version}{note}")
                upserts.append((doc, chunks))
                events += [(sop_id, old["version"], old["content_hash"], "superseded"),
                           (sop_id, doc.version, doc.content_hash, "updated")]
            elif model_changed:
                upserts.append((doc, chunks))
                report["unchanged"].append(sop_id)
            elif old["file_name"] != doc.file_name:
                # Same content under a new file name: refresh metadata only; embeddings are reused.
                report["renamed"].append(f"{sop_id}: {old['file_name']} -> {doc.file_name}")
                upserts.append((doc, chunks))
            else:
                report["unchanged"].append(sop_id)
        removals = sorted(set(active) - set(incoming))
        for sop_id in removals:
            report["withdrawn"].append(f"{sop_id} v{active[sop_id]['version']}")
            events.append((sop_id, active[sop_id]["version"], active[sop_id]["content_hash"], "withdrawn"))
        if active and len(removals) / len(active) > MAX_WITHDRAW_FRACTION and not opts.force:
            raise IngestionError(
                f"snapshot would withdraw {len(removals)} of {len(active)} SOPs; looks incomplete. "
                "Rerun with --force if intended", {"withdrawn": report["withdrawn"]})

        if not upserts and not removals:
            report["status"] = "no_changes"
            db.record_run(run_id, str(snapshot), started, "no_changes", report)
            log(logger, "ingest.no_changes", **_summary(report))
            return report

        # Embed only texts we have never embedded with this model.
        rows: list[tuple[SopDocument, list[tuple[Chunk, str]]]] = []
        need: dict[str, str] = {}
        for doc, chunks in upserts:
            keyed = [(ch, embed_key(embedder.model_id, ch.embed_text)) for ch in chunks]
            rows.append((doc, keyed))
            for ch, k in keyed:
                need[k] = ch.embed_text
        have = db.get_embeddings(list(need))
        missing = [(k, t) for k, t in need.items() if k not in have]
        report["embeddings_reused"] = len(need) - len(missing)
        if missing and not opts.dry_run:
            for i in range(0, len(missing), EMBED_BATCH):
                batch = missing[i:i + EMBED_BATCH]
                vecs = embedder.embed_documents([t for _, t in batch], correlation_id=run_id)
                report["embedding_calls"] += 1
                db.put_embeddings(embedder.model_id, {k: v for (k, _), v in zip(batch, vecs)})
        report["texts_embedded"] = 0 if opts.dry_run else len(missing)

        if opts.dry_run:
            report["status"] = "dry_run"
            return report

        report["published"] = True
        report["status"] = "published"
        report["generation"] = base_gen + 1
        db.publish(base_generation=base_gen, upserts=rows, removals=removals, events=events,
                   embed_model=embedder.model_id, run_id=run_id, snapshot=str(snapshot),
                   started_at=started, report=report, fail_after_deletes=_fail_after_deletes)
        log(logger, "ingest.published", **_summary(report))
        return report
    except IngestionError as e:
        report["status"] = "rejected"
        report["error"] = e.message
        report["details"] = e.details
        db.record_run(run_id, str(snapshot), started, "rejected", report)
        log(logger, "ingest.rejected", logging.ERROR, error=e.message, **e.details)
        raise
    except Exception as e:
        # Crash, provider outage or concurrent publish: the publish transaction was
        # rolled back (or never started), so the previous generation is still live.
        report.update(status="failed", published=False, error=f"{type(e).__name__}: {e}")
        report.pop("generation", None)
        db.record_run(run_id, str(snapshot), started, "failed", report)
        log(logger, "ingest.failed", logging.ERROR, error=report["error"])
        raise
    finally:
        correlation_id_var.reset(token)


def _summary(r: dict) -> dict:
    return {k: r[k] for k in ("added", "updated", "withdrawn", "renamed", "warnings",
                              "embedding_calls", "texts_embedded", "embeddings_reused")} | {
        "unchanged_count": len(r["unchanged"]), "generation": r.get("generation")}

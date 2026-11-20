"""SQLite persistence: active documents, active chunks, content-addressed
embeddings, index generation, ingest-run reports and the answer cache.

State model
-----------
* `documents` / `chunks` hold ONLY the currently published library. Superseded and
  withdrawn versions are deleted in the same transaction that publishes their
  replacement, so a stale chunk cannot exist after a successful run.
* `index_state.generation` is bumped by every publish that changed something. The
  answer-cache key contains it, and the in-memory vector index reloads on change.
* `embeddings` is keyed by sha256(model_id + text). It is written before publish
  (outside the publish transaction) because it is append-only and content-addressed:
  a crash leaves harmless extra rows that make the retry cheaper, never stale data.
* SQLite runs in WAL mode, so readers keep a consistent snapshot while a publish
  transaction is in progress: staff see the old library or the new one, never a mix.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

from app.domain.models import Chunk, SopDocument

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    sop_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    access_field TEXT NOT NULL,
    access_level TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    file_name TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    published_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    sop_id TEXT NOT NULL REFERENCES documents(sop_id),
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    access_level TEXT NOT NULL,
    section_no TEXT NOT NULL,
    section_title TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    embed_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_sop ON chunks(sop_id);
CREATE TABLE IF NOT EXISTS embeddings (
    embed_key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS index_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    embed_model TEXT NOT NULL DEFAULT '',
    published_at REAL
);
INSERT OR IGNORE INTO index_state (id, generation) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS document_history (
    sop_id TEXT NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    event TEXT NOT NULL,          -- added | updated | withdrawn | superseded
    generation INTEGER NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    report_json TEXT
);
CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class ConcurrentPublishError(Exception):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        yield conn

    # ---------- reads ----------
    def generation(self) -> int:
        with self.connect() as c:
            return int(c.execute("SELECT generation FROM index_state WHERE id=1").fetchone()[0])

    def embed_model(self) -> str:
        with self.connect() as c:
            return c.execute("SELECT embed_model FROM index_state WHERE id=1").fetchone()[0]

    def active_documents(self) -> dict[str, sqlite3.Row]:
        with self.connect() as c:
            return {r["sop_id"]: r for r in c.execute("SELECT * FROM documents")}

    def load_index(self) -> tuple[int, list[Chunk], np.ndarray]:
        """Read generation + all active chunks + vectors in ONE read transaction,
        so the in-memory index is always a single consistent generation."""
        with self.connect() as c:
            c.execute("BEGIN")
            try:
                gen = int(c.execute("SELECT generation FROM index_state WHERE id=1").fetchone()[0])
                rows = c.execute(
                    "SELECT ch.*, e.dim, e.vector FROM chunks ch JOIN embeddings e ON e.embed_key = ch.embed_key "
                    "ORDER BY ch.sop_id, ch.ordinal"
                ).fetchall()
            finally:
                c.execute("COMMIT")
        chunks = [
            Chunk(chunk_id=r["chunk_id"], sop_id=r["sop_id"], version=r["version"], title=r["title"],
                  access_level=r["access_level"], section_no=r["section_no"], section_title=r["section_title"],
                  page_start=r["page_start"], page_end=r["page_end"], text=r["text"], ordinal=r["ordinal"])
            for r in rows
        ]
        if rows:
            mat = np.vstack([np.frombuffer(r["vector"], dtype=np.float32, count=r["dim"]) for r in rows])
        else:
            mat = np.zeros((0, 1), dtype=np.float32)
        return gen, chunks, mat

    def get_embeddings(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self.connect() as c:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                q = f"SELECT embed_key, dim, vector FROM embeddings WHERE embed_key IN ({','.join('?' * len(part))})"
                for r in c.execute(q, part):
                    out[r["embed_key"]] = np.frombuffer(r["vector"], dtype=np.float32, count=r["dim"])
        return out

    # ---------- writes ----------
    def put_embeddings(self, model_id: str, items: dict[str, np.ndarray]) -> None:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.executemany(
                "INSERT OR IGNORE INTO embeddings (embed_key, model_id, dim, vector) VALUES (?,?,?,?)",
                [(k, model_id, int(v.shape[0]), v.astype(np.float32).tobytes()) for k, v in items.items()],
            )
            c.execute("COMMIT")

    def publish(
        self,
        *,
        base_generation: int,
        upserts: list[tuple[SopDocument, list[tuple[Chunk, str]]]],
        removals: list[str],
        events: list[tuple[str, str, str, str]],
        embed_model: str,
        run_id: str,
        snapshot: str,
        started_at: float,
        report: dict,
        fail_after_deletes: bool = False,   # test hook: simulate a crash mid-publish
    ) -> int:
        """Apply a whole snapshot diff atomically. Returns the new generation."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                gen = int(c.execute("SELECT generation FROM index_state WHERE id=1").fetchone()[0])
                if gen != base_generation:
                    raise ConcurrentPublishError(
                        f"index moved from generation {base_generation} to {gen} during this run")
                new_gen = gen + 1
                for sop_id in removals + [d.sop_id for d, _ in upserts]:
                    c.execute("DELETE FROM chunks WHERE sop_id=?", (sop_id,))
                    c.execute("DELETE FROM documents WHERE sop_id=?", (sop_id,))
                if fail_after_deletes:
                    raise RuntimeError("simulated crash during publish")
                for doc, chunk_rows in upserts:
                    c.execute(
                        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (doc.sop_id, doc.version, doc.title, doc.effective_date, doc.access_field,
                         doc.access_level, doc.content_hash, doc.file_name, doc.page_count,
                         doc.change_note, new_gen),
                    )
                    c.executemany(
                        "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(ch.chunk_id, ch.sop_id, ch.version, ch.title, ch.access_level, ch.section_no,
                          ch.section_title, ch.page_start, ch.page_end, ch.ordinal, ch.text, key)
                         for ch, key in chunk_rows],
                    )
                now = time.time()
                c.executemany(
                    "INSERT INTO document_history VALUES (?,?,?,?,?,?)",
                    [(s, v, h, ev, new_gen, now) for s, v, h, ev in events],
                )
                c.execute("UPDATE index_state SET generation=?, embed_model=?, published_at=? WHERE id=1",
                          (new_gen, embed_model, now))
                # Cache entries from older generations can never be hit again (the key
                # contains the generation); delete them in the same transaction.
                c.execute("DELETE FROM answer_cache WHERE generation < ?", (new_gen,))
                c.execute("INSERT INTO ingest_runs VALUES (?,?,?,?,?,?)",
                          (run_id, snapshot, started_at, now, "published", json.dumps(report)))
                c.execute("COMMIT")
                return new_gen
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def record_run(self, run_id: str, snapshot: str, started_at: float, status: str, report: dict) -> None:
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO ingest_runs VALUES (?,?,?,?,?,?)",
                      (run_id, snapshot, started_at, time.time(), status, json.dumps(report)))

    # ---------- answer cache ----------
    def cache_get(self, key: str, generation: int, ttl_s: int) -> str | None:
        with self.connect() as c:
            r = c.execute("SELECT response_json, generation, created_at FROM answer_cache WHERE cache_key=?",
                          (key,)).fetchone()
        if r is None or r["generation"] != generation or time.time() - r["created_at"] > ttl_s:
            return None
        return r["response_json"]

    def cache_put(self, key: str, generation: int, response_json: str) -> None:
        with self.connect() as c:
            # Only store if the generation is still current, so a request that started
            # before a publish cannot write an old-generation answer after it.
            c.execute(
                "INSERT OR REPLACE INTO answer_cache (cache_key, generation, response_json, created_at) "
                "SELECT ?, ?, ?, ? WHERE (SELECT generation FROM index_state WHERE id=1) = ?",
                (key, generation, response_json, time.time(), generation),
            )

import shutil

import pytest

from app.domain.errors import IngestionError
from app.ingest.indexer import IngestOptions, run_ingestion
from app.store.db import ConcurrentPublishError
from tests.conftest import SOPS, UPDATE
from tests.pdf_factory import make_sop_pdf


def active(c):
    return {k: r["version"] for k, r in c.db.active_documents().items()}


def chunk_versions(c):
    _, chunks, _ = c.db.load_index()
    return {(ch.sop_id, ch.version) for ch in chunks}


def test_initial_ingest(container):
    assert len(active(container)) == 15
    assert container.db.generation() == 1


def test_update_snapshot_diff_is_exact(container):
    rep = run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    assert [a.split()[0] for a in rep["added"]] == ["SOP-16", "SOP-17"]
    assert [u.split()[0] for u in rep["updated"]] == ["SOP-02", "SOP-08", "SOP-11"]
    assert rep["withdrawn"] == ["SOP-15 v1.1"]
    assert [r.split(":")[0] for r in rep["renamed"]] == ["SOP-05"]
    assert "SOP-03" in rep["unchanged"]                 # re-exported PDF, same text: not re-embedded
    assert rep["generation"] == 2
    # Only new chunk texts were embedded; unchanged sections of SOP-02/08/11 reuse vectors.
    assert 0 < rep["texts_embedded"] < 20 and rep["embeddings_reused"] > 0


def test_no_stale_chunks_after_update(container):
    run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    versions = chunk_versions(container)
    assert ("SOP-11", "1.2") not in versions and ("SOP-11", "2.0") in versions
    assert ("SOP-02", "2.3") not in versions and ("SOP-08", "2.0") not in versions
    assert not any(s == "SOP-15" for s, _ in versions)
    assert {s for s, _ in versions} == {f"SOP-{i:02d}" for i in range(1, 18) if i != 15}
    # The renamed SOP-05 exists exactly once.
    _, chunks, _ = container.db.load_index()
    sop05 = [c for c in chunks if c.sop_id == "SOP-05"]
    assert len(sop05) == len({c.section_no for c in sop05})


def test_second_run_is_a_free_noop(container):
    run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    calls, gen = container.embedder.calls, container.db.generation()
    rep = run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    assert rep["status"] == "no_changes"
    assert container.embedder.calls == calls and container.db.generation() == gen


def test_crash_mid_publish_leaves_old_library_intact(container):
    before = chunk_versions(container)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_ingestion(str(UPDATE), container.db, container.embedder, container.policy, _fail_after_deletes=True)
    assert container.db.generation() == 1
    assert chunk_versions(container) == before            # no mix of old and new
    # Retry succeeds and reuses the embeddings computed before the crash.
    calls = container.embedder.calls
    rep = run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    assert rep["status"] == "published" and rep["texts_embedded"] == 0
    assert container.embedder.calls == calls


def test_concurrent_publish_is_detected(container, monkeypatch):
    real = container.db.publish

    def racing_publish(**kw):
        with container.db.connect() as conn:          # another run publishes first
            conn.execute("UPDATE index_state SET generation = generation + 1")
        return real(**kw)

    monkeypatch.setattr(container.db, "publish", racing_publish)
    with pytest.raises(ConcurrentPublishError):
        run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)


def test_identity_is_sop_id_not_file_name(container, snapshot_copy):
    snap = snapshot_copy(SOPS, "renamed_all")
    for i, p in enumerate(sorted(snap.glob("*.pdf"))):
        p.rename(p.with_name(f"dms_export_{i:03d}.pdf"))
    rep = run_ingestion(str(snap), container.db, container.embedder, container.policy)
    assert len(rep["renamed"]) == 15 and not rep["added"] and not rep["withdrawn"]
    assert rep["texts_embedded"] == 0


def test_old_and_new_copy_in_same_snapshot_uses_newer(container, snapshot_copy):
    snap = snapshot_copy(UPDATE, "dupes")
    shutil.copy(SOPS / "SOP-08_Patient_Complaints_and_Grievance_Handling.pdf", snap / "SOP-08_old_copy.pdf")
    rep = run_ingestion(str(snap), container.db, container.embedder, container.policy)
    assert active(container)["SOP-08"] == "2.1"
    assert any("SOP-08" in w and "using v2.1" in w for w in rep["warnings"])


def test_same_version_different_content_rejects_snapshot(container, snapshot_copy, tmp_path):
    snap = snapshot_copy(SOPS, "conflicting")
    make_sop_pdf(snap / "SOP-90_a.pdf", sop_id="SOP-90", sections=[("Purpose", "A."), ("Rule", "One.")])
    make_sop_pdf(snap / "SOP-90_b.pdf", sop_id="SOP-90", sections=[("Purpose", "A."), ("Rule", "Two.")])
    with pytest.raises(IngestionError):
        run_ingestion(str(snap), container.db, container.embedder, container.policy)
    assert container.db.generation() == 1 and "SOP-90" not in active(container)


def test_one_bad_file_rejects_whole_snapshot(container, snapshot_copy):
    snap = snapshot_copy(UPDATE, "bad")
    (snap / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(IngestionError) as e:
        run_ingestion(str(snap), container.db, container.embedder, container.policy)
    assert any("broken.pdf" in m for m in e.value.details["errors"])
    assert active(container)["SOP-02"] == "2.3"          # nothing published


def test_unknown_access_value_rejects_snapshot(container, snapshot_copy):
    snap = snapshot_copy(SOPS, "badaccess")
    make_sop_pdf(snap / "SOP-92.pdf", sop_id="SOP-92", access="Managers")
    with pytest.raises(IngestionError):
        run_ingestion(str(snap), container.db, container.embedder, container.policy)


def test_mass_withdrawal_guard(container, tmp_path):
    partial = tmp_path / "partial"
    partial.mkdir()
    for p in sorted(SOPS.glob("*.pdf"))[:3]:
        shutil.copy(p, partial / p.name)
    with pytest.raises(IngestionError, match="looks incomplete"):
        run_ingestion(str(partial), container.db, container.embedder, container.policy)
    rep = run_ingestion(str(partial), container.db, container.embedder, container.policy, IngestOptions(force=True))
    assert len(rep["withdrawn"]) == 12


def test_empty_snapshot_rejected(container, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(IngestionError):
        run_ingestion(str(tmp_path / "empty"), container.db, container.embedder, container.policy)


def test_downgrade_rejected_unless_allowed(container):
    run_ingestion(str(UPDATE), container.db, container.embedder, container.policy)
    with pytest.raises(IngestionError, match="downgrade"):
        run_ingestion(str(SOPS), container.db, container.embedder, container.policy)
    rep = run_ingestion(str(SOPS), container.db, container.embedder, container.policy,
                        IngestOptions(allow_downgrade=True, force=True))
    assert active(container)["SOP-02"] == "2.3" and rep["status"] == "published"


def test_dry_run_changes_nothing(container):
    rep = run_ingestion(str(UPDATE), container.db, container.embedder, container.policy, IngestOptions(dry_run=True))
    assert rep["status"] == "dry_run" and container.db.generation() == 1 and "SOP-16" not in active(container)

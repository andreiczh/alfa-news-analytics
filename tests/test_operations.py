import gzip
import json
from pathlib import Path
import shutil
import time

import pytest

from alfa_agent.operations import backup, restore_into_empty
from alfa_agent.service import Service
from alfa_agent.settings import Settings
from alfa_agent.storage import Conflict


def empty_service(tmp_path):
    (tmp_path / "data").mkdir(parents=True)
    shutil.copy2(Path(__file__).parents[1] / "data/demo_bundle.json", tmp_path / "data/demo_bundle.json")
    return Service(Settings(root=tmp_path))


def test_backup_restores_originals_context_and_accounts_without_replaying_sessions(service, tmp_path):
    me = service.accounts.register(
        "backup-account", "synthetic-private-password", "Synthetic User", "Synthetic Company", "CFO"
    )
    cid = me.company_id
    service.workspace.save_context(cid, "Synthetic preserved context", "")
    document, _ = service.workspace.add_file(cid, "synthetic.txt", b"Synthetic exact original")
    service.accounts.new_session(me)
    path = tmp_path / "private-backup.gz"
    saved = backup(service.store, path)
    assert path.stat().st_mode & 0o777 == 0o600
    restored = empty_service(tmp_path / "restored")
    try:
        result = restore_into_empty(restored.store, path)
        assert result["restored_rows"] == saved["rows"]
        assert restored.accounts.login("backup-account", "synthetic-private-password").user_id == me.user_id
        assert restored.workspace.context(cid)["description"] == "Synthetic preserved context"
        row = restored.workspace.file_row(cid, document["document_id"])
        assert restored.workspace.file_path(row).read_bytes() == b"Synthetic exact original"
        with restored.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0] == 0
        with pytest.raises(Conflict, match="restore_requires_empty_runtime"):
            restore_into_empty(restored.store, path)
    finally:
        restored.corpus.close()
        restored.store.close()


def test_corrupted_backup_never_partially_writes_the_target(service, tmp_path):
    company = service.workspace.create_company("Synthetic backup company")
    path = tmp_path / "original.gz"
    backup(service.store, path)
    content = gzip.decompress(path.read_bytes()).replace(
        company["company_id"].encode(), b"company_tampered", 1
    )
    bad = tmp_path / "bad.gz"
    bad.write_bytes(gzip.compress(content))
    restored = empty_service(tmp_path / "empty")
    try:
        with pytest.raises(ValueError, match="backup_integrity_failed"):
            restore_into_empty(restored.store, bad)
        assert restored.workspace.list_custom() == []
    finally:
        restored.corpus.close()


def test_database_readiness_and_worker_staleness_are_separate(service, monkeypatch):
    assert not service.operations.readiness(True)["ready"]
    assert service.operations.readiness(False)["ready"]
    service.operations.heartbeat("synthetic-worker")
    assert service.operations.readiness(True)["ready"]
    now = time.time()
    monkeypatch.setattr("alfa_agent.operations.time.time", lambda: now + 40)
    assert not service.operations.readiness(True)["ready"]


def test_interrupted_calls_are_not_automatically_replayed_after_restore(service, tmp_path):
    from alfa_agent.service import CreateRun

    job = service.create(CreateRun(company_id="demo_floating"), "backup-interrupted")
    with service.store.connection() as db:
        row = db.execute("SELECT state_json FROM runs WHERE id=?", (job["run_id"],)).fetchone()
        state = json.loads(row[0])
        state["status"] = "RUNNING"
        db.execute(
            "UPDATE runs SET status='RUNNING',state_json=? WHERE id=?", (json.dumps(state), job["run_id"])
        )
    path = tmp_path / "running.gz"
    backup(service.store, path)
    restored = empty_service(tmp_path / "restored-running")
    try:
        restore_into_empty(restored.store, path)
        result = restored.get(job["run_id"])
        assert result["status"] == "FAILED" and result["error_code"] == "interrupted_manual_retry"
        assert restored.store.claim() is None
    finally:
        restored.corpus.close()

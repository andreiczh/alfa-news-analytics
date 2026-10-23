import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from fastapi.testclient import TestClient
import pytest

from alfa_agent.access import COOKIE, issue_session, valid_session
from alfa_agent.api import create_app
from alfa_agent.bootstrap import prepare_state
from alfa_agent.service import Service
from alfa_agent.settings import Settings


PASSWORD = "synthetic-test-password-only"


def test_team_session_is_signed_expiring_and_invalidated_by_password_change(monkeypatch):
    monkeypatch.setattr("alfa_agent.access.time.time", lambda: 1_000_000)
    session = issue_session(PASSWORD)
    assert valid_session(session, PASSWORD)
    assert not valid_session(session + "x", PASSWORD)
    assert not valid_session(session, PASSWORD + "changed")
    assert not valid_session("untrusted", PASSWORD)
    monkeypatch.setattr("alfa_agent.access.time.time", lambda: 1_043_201)
    assert not valid_session(session, PASSWORD)


def test_hosted_login_blocks_data_but_allows_health_and_secure_login(service, monkeypatch):
    monkeypatch.setenv("ALFA_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", PASSWORD)
    monkeypatch.setenv("ALFA_SESSION_SECRET", "synthetic-server-secret-never-use-outside-tests")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "test-app.onrender.com")
    app = create_app(service.settings)
    with TestClient(app, base_url="https://test-app.onrender.com") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/companies").status_code == 401
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/auth/login"
        assert "{{APP_TOKEN}}" not in client.get("/auth/login").text
        assert client.post("/auth/login", data={"password": "wrong"}).status_code == 401
        assert (
            client.post(
                "/auth/login", data={"password": PASSWORD}, headers={"Origin": "https://outside.example"}
            ).status_code
            == 403
        )
        response = client.post(
            "/auth/login",
            data={
                "password": PASSWORD,
                "full_name": "Тестовый Пользователь",
                "company_name": "Тестовая компания",
                "job_title": "CFO",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert COOKIE in client.cookies
        page = client.get("/")
        assert page.status_code == 200 and "{{APP_TOKEN}}" not in page.text
        # Login alone does not bypass the app's separate CSRF token.
        assert client.get("/api/companies").status_code == 401
        assert client.get("/api/companies", headers={"x-app-token": app.state.token}).status_code == 200
        assert client.get("/api/health", headers={"Host": "outside.example"}).status_code == 400


def test_hosted_start_fails_closed_without_strong_password(service, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("ALFA_ACCESS_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="strong_access_password"):
        create_app(service.settings)


def test_state_and_uploads_survive_new_service_with_new_code_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("ALFA_CASE_DATA_DIR", raising=False)
    code_a, code_b, disk = tmp_path / "release-a", tmp_path / "release-b", tmp_path / "persistent"
    for code in (code_a, code_b):
        (code / "data").mkdir(parents=True)
        shutil.copyfile(Path(__file__).parents[1] / "data/demo_bundle.json", code / "data/demo_bundle.json")
    first = Service(Settings(root=code_a, state_dir=disk))
    company = first.workspace.create_company("СИНТЕТИЧЕСКАЯ проверка сохранения")
    upload, _ = first.workspace.add_file(company["company_id"], "source.txt", b"Synthetic fixture only")
    first.corpus.close()
    second = Service(Settings(root=code_b, state_dir=disk))
    assert second.workspace.custom_exists(company["company_id"])
    row = second.workspace.file_row(company["company_id"], upload["document_id"])
    assert (second.workspace.upload_dir / row["stored_name"]).read_bytes() == b"Synthetic fixture only"
    assert not (code_b / "var").exists()
    second.corpus.close()


def test_bootstrap_integrity_and_existing_state_is_never_overwritten(tmp_path):
    seed, disk = tmp_path / "deploy/seed", tmp_path / "disk"
    (seed / "uploads").mkdir(parents=True)
    with sqlite3.connect(seed / "agent.sqlite3") as db:
        db.execute("CREATE TABLE marker(value TEXT)")
        db.execute("INSERT INTO marker VALUES('original')")
    (seed / "uploads/file.txt").write_text("synthetic")
    manifest = {
        "files": {
            name: hashlib.sha256((seed / name).read_bytes()).hexdigest()
            for name in ("agent.sqlite3", "uploads/file.txt")
        }
    }
    (seed / "manifest.json").write_text(json.dumps(manifest))
    settings = Settings(root=tmp_path, state_dir=disk)
    prepare_state(settings)
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE marker SET value='user change'")
    prepare_state(settings)
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "user change"
    assert (disk / "uploads/file.txt").read_text() == "synthetic"
    settings.db_path.unlink()
    (seed / "uploads/file.txt").write_text("corrupted")
    with pytest.raises(ValueError, match="seed_integrity_error"):
        prepare_state(settings)


def test_health_detects_dead_attached_worker(service):
    class DeadWorker:
        def poll(self):
            return 1

    app = create_app(service.settings)
    app.state.worker_process = DeadWorker()
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 503

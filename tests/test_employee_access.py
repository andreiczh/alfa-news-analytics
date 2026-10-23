import asyncio

from fastapi.testclient import TestClient

from alfa_agent.api import create_app
from alfa_agent.engine import Engine
from alfa_agent.identity import Principal, current_principal
from alfa_agent.service import CreateRun
from conftest import ScenarioGateway
from alfa_agent.access import COOKIE, issue_session
from alfa_agent.settings import Settings


PASSWORD = "synthetic-team-password-only"


def login(client, name="Тестовый Пользователь", company="Одинаковая компания", title="CFO"):
    response = client.post(
        "/auth/login",
        data={"password": PASSWORD, "full_name": name, "company_name": company, "job_title": title},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_login_records_person_company_and_role_and_does_not_join_by_name(service, monkeypatch):
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", PASSWORD)
    app = create_app(service.settings)
    with TestClient(app) as client:
        client.headers["x-app-token"] = app.state.token
        login(client)
        a = client.get("/api/me").json()
        cid = a["company_id"]
        assert a["full_name"] == "Тестовый Пользователь" and a["role"] == "cfo"
        legitimate = client.cookies.get(COOKIE)
        forged = issue_session(
            PASSWORD,
            Principal(
                **{
                    k: a[k]
                    for k in ("user_id", "company_id", "full_name", "company_name", "job_title", "role")
                }
            ),
        )
        client.cookies.set(COOKIE, forged, domain="testserver.local", path="/")
        assert client.get("/api/me").status_code == 401
        client.cookies.set(COOKIE, legitimate, domain="testserver.local", path="/")
        response = client.post(
            f"/api/companies/{cid}/documents",
            data={"bound_company_id": cid},
            files={"file": ("private.txt", b"Private synthetic document")},
        )
        assert response.status_code == 202
        did = response.json()["document"]["document_id"]
        run = client.post(
            "/api/runs", headers={"Idempotency-Key": "private-run"}, json={"company_id": "demo_floating"}
        ).json()
        client.cookies.clear()
        login(client)
        b = client.get("/api/me").json()
        assert b["company_id"] != cid and b["user_id"] != a["user_id"]
        assert all(c["company_id"] != cid for c in client.get("/api/companies").json())
        assert client.get(f"/api/companies/{cid}/profile").status_code == 403
        assert client.get(f"/api/companies/{cid}/documents/{did}/download").status_code == 403
        assert client.get("/api/runs/" + run["run_id"]).status_code == 404
        assert (
            client.put(f"/api/companies/{cid}/context", json={"description": "Intrusion"}).status_code == 403
        )
        assert client.get("/api/companies/demo_floating/profile").status_code == 200
        assert (
            client.post(
                "/api/companies/demo_floating/documents",
                data={"bound_company_id": "demo_floating"},
                files={"file": ("bad.txt", b"overwrite sample")},
            ).status_code
            == 403
        )


def test_worker_can_complete_employee_owned_run_without_request_context(service):
    principal = Principal("user_synthetic", "company_synthetic", "Тестовый Сотрудник", "Тест", "CFO", "cfo")
    token = current_principal.set(principal)
    try:
        run = service.create(CreateRun(company_id="demo_floating"), "tenant-case")
    finally:
        current_principal.reset(token)
    engine = Engine(service.settings, service.store, service.corpus, ScenarioGateway())
    asyncio.run(engine.once(run["run_id"]))
    token = current_principal.set(principal)
    try:
        assert service.get(run["run_id"])["status"] == "SUCCEEDED"
    finally:
        current_principal.reset(token)


def test_manual_company_context_is_self_reported_and_snapshot_is_frozen(service):
    company = service.workspace.create_company("Контекст — синтетика")
    cid = company["company_id"]
    service.workspace.save_context(cid, "Производим упаковку, есть импортные закупки.", "https://example.com")
    first = service.workspace.snapshot(cid, service.corpus, persist=True)
    feature = next(f for f in first["profile"]["features"] if f["name"] == "company_description")
    assert feature["status"] == "self_reported"
    service.workspace.save_context(cid, "Описание обновлено пользователем.", "")
    stored = service.workspace.load_snapshot(first["snapshot_id"], cid)
    assert stored == {k: v for k, v in first.items() if k != "snapshot_id"}


def test_signed_identity_survives_cold_restart_but_lost_uploads_are_not_claimed_restored(
    service, monkeypatch
):
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", PASSWORD)
    monkeypatch.setenv("ALFA_SESSION_SECRET", "synthetic-stable-server-secret-for-restart-test")
    first = create_app(service.settings)
    with TestClient(first) as client:
        client.headers["x-app-token"] = first.state.token
        login(client)
        identity = client.get("/api/me").json()
        cookie = client.cookies.get(COOKIE)
    second = create_app(
        Settings(root=service.settings.root, state_dir=service.settings.root / "blank-instance")
    )
    with TestClient(second) as client:
        client.headers["x-app-token"] = second.state.token
        client.cookies.set(COOKIE, cookie)
        assert client.get("/api/me").json()["user_id"] == identity["user_id"]
        assert client.get("/api/companies/" + identity["company_id"] + "/profile").status_code == 200
        assert client.get("/api/companies/" + identity["company_id"] + "/documents").json() == []

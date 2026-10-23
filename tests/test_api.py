import asyncio

from fastapi.testclient import TestClient

from alfa_agent.api import create_app
from alfa_agent.engine import Engine
from conftest import ScenarioGateway


def client_for(service):
    app = create_app(service.settings)
    return app, TestClient(app)


def test_ui_and_health_do_not_expose_provider_key(service):
    app, client = client_for(service)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert "test-only-value" not in page.text
        assert "{{APP_TOKEN}}" not in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        health = client.get("/api/health")
        assert health.json()["key_configured"] is True
        assert "test-only-value" not in health.text
        assert client.get("/static/app.js").status_code == 200


def test_local_session_and_origin_are_required(service):
    app, client = client_for(service)
    with client:
        assert client.get("/api/companies").status_code == 401
        headers = {"X-App-Token": app.state.token}
        assert client.get("/api/companies", headers=headers).status_code == 200
        assert (
            client.get("/api/companies", headers={**headers, "Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_api_start_poll_sources_export_and_feedback(service):
    app, client = client_for(service)
    with client:
        headers = {"X-App-Token": app.state.token, "Idempotency-Key": "api-e2e"}
        created = client.post("/api/runs", headers=headers, json={})
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert client.get(f"/api/runs/{run_id}/export", headers=headers).status_code == 409
        asyncio.run(Engine(service.settings, service.store, service.corpus, ScenarioGateway()).once())
        result = client.get(f"/api/runs/{run_id}", headers=headers).json()
        assert result["status"] == "SUCCEEDED"
        sources = client.get(f"/api/runs/{run_id}/sources", headers=headers).json()
        assert sources["profile"]["company_id"] == "demo_floating"
        assert not any(d.get("tenant_company_id") == "demo_fixed" for d in sources["documents"])
        assert client.get(f"/api/runs/{run_id}/export", headers=headers).status_code == 200
        assert (
            client.post(
                f"/api/runs/{run_id}/feedback", headers=headers, json={"rating": "useful"}
            ).status_code
            == 201
        )


def test_extra_access_fields_and_unknown_companies_rejected(service):
    app, client = client_for(service)
    with client:
        headers = {"X-App-Token": app.state.token, "Idempotency-Key": "deny"}
        assert client.post("/api/runs", headers=headers, json={"tenant_id": "another"}).status_code == 422
        assert client.post("/api/runs", headers=headers, json={"company_id": "another"}).status_code == 403
        assert client.post("/api/runs", headers=headers, json={"horizon_days": 9999}).status_code == 422
        assert client.get("/api/runs/unknown", headers=headers).status_code == 404


def test_api_repeated_create_and_cancel(service):
    app, client = client_for(service)
    with client:
        headers = {"X-App-Token": app.state.token, "Idempotency-Key": "same"}
        first = client.post("/api/runs", headers=headers, json={}).json()
        second = client.post("/api/runs", headers=headers, json={}).json()
        assert first["run_id"] == second["run_id"]
        assert client.post("/api/runs", headers=headers, json={"company_id": "demo_fixed"}).status_code == 409
        canceled = client.post(f"/api/runs/{first['run_id']}/cancel", headers=headers).json()
        assert canceled["status"] == "CANCELLED"

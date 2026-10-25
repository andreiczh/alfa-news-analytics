import re

from fastapi.testclient import TestClient
import pytest

from alfa_agent.accounts import AuthLimited, password_hash, password_matches
from alfa_agent.access import COOKIE
from alfa_agent.api import create_app


ACCESS = "synthetic-pilot-invitation-code"
PASSWORD = "synthetic-individual-password-01"


@pytest.fixture
def app(service, monkeypatch):
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", ACCESS)
    monkeypatch.setenv("ALFA_AUTH_MODE", "accounts")
    return create_app(service.settings)


def csrf(client):
    token = re.search(r'name="app-token" content="([^"]+)"', client.get("/").text)[1]
    client.headers["X-App-Token"] = token
    return token


def register(client, login):
    response = client.post(
        "/auth/register",
        data={
            "login": login,
            "password": PASSWORD,
            "full_name": "Тестовый Пользователь",
            "company_name": "Одинаковая компания",
            "job_title": "CFO",
            "invite_password": ACCESS,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    csrf(client)
    return client.get("/api/me").json()


def test_passwords_are_salted_and_wrong_values_fail():
    a, b = password_hash(PASSWORD), password_hash(PASSWORD)
    assert a != b and PASSWORD not in a
    assert password_matches(PASSWORD, a)
    assert not password_matches(PASSWORD + "wrong", a)
    assert not password_matches(PASSWORD, "invalid")


def test_login_returns_the_same_workspace_and_logout_revokes_replay(app):
    with TestClient(app) as client:
        first = register(client, "account-one")
        cid = first["company_id"]
        assert (
            client.put(
                f"/api/companies/{cid}/context", json={"description": "Synthetic persistent context"}
            ).status_code
            == 200
        )
        cookie = client.cookies.get(COOKIE)
        assert client.post("/auth/logout").status_code == 200
        client.cookies.set(COOKIE, cookie)
        assert client.get("/api/me").status_code == 401
        client.cookies.clear()
        assert (
            client.post(
                "/auth/login", data={"login": "account-one", "password": PASSWORD}, follow_redirects=False
            ).status_code
            == 303
        )
        csrf(client)
        second = client.get("/api/me").json()
        assert second["user_id"] == first["user_id"] and second["company_id"] == cid
        assert "Synthetic persistent context" in client.get(f"/api/companies/{cid}/profile").text


def test_workspace_permission_is_separate_from_cfo_role_and_is_revocable(app):
    with TestClient(app) as owner, TestClient(app) as colleague:
        a, b = register(owner, "workspace-owner"), register(colleague, "workspace-viewer")
        cid = a["company_id"]
        assert cid != b["company_id"]
        assert colleague.get(f"/api/companies/{cid}/profile").status_code == 403
        invite = owner.post(f"/api/companies/{cid}/team/invites", json={"permission": "viewer"}).json()
        assert colleague.post("/api/account/join", json={"code": invite["code"]}).status_code == 200
        assert colleague.get(f"/api/companies/{cid}/profile").status_code == 200
        assert (
            colleague.put(f"/api/companies/{cid}/context", json={"description": "Not allowed"}).status_code
            == 403
        )
        assert (
            colleague.put("/api/me/preferences", json={"company_id": cid, "role": "ceo"}).status_code == 200
        )
        assert (
            colleague.put(
                f"/api/companies/{cid}/context", json={"description": "Still not allowed"}
            ).status_code
            == 403
        )
        assert (
            colleague.post(f"/api/companies/{cid}/team/invites", json={"permission": "member"}).status_code
            == 403
        )
        assert colleague.post("/api/account/join", json={"code": invite["code"]}).status_code == 400
        assert owner.delete(f"/api/companies/{cid}/team/{b['user_id']}").status_code == 200
        assert colleague.get(f"/api/companies/{cid}/profile").status_code == 403
        assert owner.delete(f"/api/companies/{cid}/team/{a['user_id']}").status_code == 403
        assert colleague.get("/api/me").json()["selected_company_id"] == b["company_id"]


def test_password_change_invalidates_existing_sessions(app):
    with TestClient(app) as client:
        first = register(client, "password-change")
        old_cookie = client.cookies.get(COOKIE)
        assert (
            client.put(
                "/api/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD + "new"}
            ).status_code
            == 200
        )
        client.cookies.set(COOKIE, old_cookie)
        assert client.get("/api/me").status_code == 401
        client.cookies.clear()
        assert (
            client.post(
                "/auth/login", data={"login": "password-change", "password": PASSWORD}, follow_redirects=False
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/auth/login",
                data={"login": "password-change", "password": PASSWORD + "new"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        csrf(client)
        assert client.get("/api/me").json()["user_id"] == first["user_id"]


def test_csrf_token_from_another_session_does_not_work(app):
    with TestClient(app) as a, TestClient(app) as b:
        register(a, "csrf-one")
        register(b, "csrf-two")
        b.headers["X-App-Token"] = a.headers["X-App-Token"]
        assert b.get("/api/me").status_code == 401


def test_login_limits_are_committed_even_on_rejection(service):
    service.accounts.limit("test", "synthetic-account", 1, 900)
    with pytest.raises(AuthLimited):
        service.accounts.limit("test", "synthetic-account", 1, 900)
    with pytest.raises(AuthLimited):
        service.accounts.limit("test", "synthetic-account", 1, 900)


def test_disabled_account_and_shared_invitation_code_do_not_authenticate(app):
    with TestClient(app) as client:
        me = register(client, "disabled-account")
        with app.state.service.store.connection() as db:
            db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (me["user_id"],))
        assert client.get("/api/me").status_code == 401
        client.cookies.clear()
        assert (
            client.post(
                "/auth/login", data={"login": "disabled-account", "password": ACCESS}, follow_redirects=False
            ).status_code
            == 401
        )


def test_inflight_old_password_login_cannot_mint_a_session_after_password_reset(service, monkeypatch):
    from alfa_agent.accounts import AuthError

    principal = service.accounts.register(
        "racing-login", PASSWORD, "Synthetic User", "Synthetic Company", "CFO"
    )
    original = service.accounts.new_session

    def reset_before_session(principal, *, expected_auth_version=None):
        with service.store.connection() as db:
            db.execute("UPDATE accounts SET auth_version=auth_version+1 WHERE id=?", (principal.user_id,))
        return original(principal, expected_auth_version=expected_auth_version)

    monkeypatch.setattr(service.accounts, "new_session", reset_before_session)
    with pytest.raises(AuthError, match="изменены"):
        service.accounts.login("racing-login", PASSWORD, with_session=True)
    with service.store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM account_sessions WHERE user_id=?", (principal.user_id,)
            ).fetchone()[0]
            == 0
        )

from alfa_agent.desktop import load_desktop_access


def test_desktop_config_loads_only_access_fields_without_overriding_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", "existing-test-password")
    monkeypatch.delenv("ALFA_SESSION_SECRET", raising=False)
    monkeypatch.delenv("UNRELATED_INSTRUCTION", raising=False)
    (tmp_path / ".env.render").write_text(
        "ALFA_ACCESS_PASSWORD=ignored\nALFA_SESSION_SECRET=synthetic-server-secret\nUNRELATED_INSTRUCTION=must-not-load\n"
    )
    load_desktop_access(tmp_path)
    import os

    assert os.environ["ALFA_ACCESS_PASSWORD"] == "existing-test-password"
    assert os.environ["ALFA_SESSION_SECRET"] == "synthetic-server-secret"
    assert "UNRELATED_INSTRUCTION" not in os.environ

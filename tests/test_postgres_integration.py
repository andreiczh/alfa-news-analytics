"""Opt-in real PostgreSQL/TLS tests; each test owns only randomly named schemas."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import uuid

import pytest

from alfa_agent.identity import create_principal, current_principal
from alfa_agent.operations import backup, restore_into_empty
from alfa_agent.service import Service
from alfa_agent.settings import Settings


@pytest.fixture
def pg_services(tmp_path):
    url = os.environ.get("ALFA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ALFA_TEST_DATABASE_URL is needed for actual PostgreSQL acceptance")
    import certifi
    import psycopg
    from psycopg import sql

    kwargs = {
        "sslmode": "verify-full",
        "sslrootcert": os.environ.get("ALFA_DATABASE_CA_FILE") or certifi.where(),
    }
    created = []

    def make(with_seed=False, seed_examples=True):
        schema = "qa_" + uuid.uuid4().hex
        root = tmp_path / schema
        (root / "data").mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "data/demo_bundle.json", root / "data/demo_bundle.json")
        if with_seed:
            shutil.copytree(
                Path(__file__).parents[1] / "deploy/seed",
                root / "deploy/seed",
                ignore=shutil.ignore_patterns("*-wal", "*-shm"),
            )
        with psycopg.connect(url, **kwargs) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        service = Service(
            Settings(root=root, database_url=url, database_schema=schema), seed_examples=seed_examples
        )
        created.append((schema, service))
        return service

    yield make
    for schema, service in reversed(created):
        service.corpus.close()
        service.store.close()
        with psycopg.connect(url, **kwargs) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_only_packaged_public_examples_are_seeded_once(pg_services):
    from alfa_agent.bootstrap import seed_postgres_examples
    from alfa_agent.identity import PUBLIC_DEMO_RUNS, PUBLIC_DEMO_COMPANIES

    service = pg_services(with_seed=True)
    seed_postgres_examples(service)
    with service.store.connection() as db:
        assert {r[0] for r in db.execute("SELECT id FROM runs")} == PUBLIC_DEMO_RUNS
        assert {r[0] for r in db.execute("SELECT id FROM custom_companies")} == PUBLIC_DEMO_COMPANIES
        assert db.execute("SELECT count(*) FROM employee_profiles").fetchone()[0] == 0
    company = next(iter(PUBLIC_DEMO_COMPANIES))
    files = service.workspace.files(company)
    assert files
    for document in files:
        row = service.workspace.file_row(company, document["document_id"])
        assert service.workspace.file_path(row).is_file()


def test_restore_can_skip_packaged_seed_in_an_actual_release_root(pg_services, tmp_path):
    original = pg_services(with_seed=True)
    saved = tmp_path / "seeded-runtime.gz"
    backup(original.store, saved)
    target = pg_services(with_seed=True, seed_examples=False)
    restored = restore_into_empty(target.store, saved)
    assert restored["restored_rows"] > 0
    with target.store.connection() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_pg_commit_rollback_and_blob_cache_survive_a_new_service(pg_services, tmp_path):
    service = pg_services()
    cid = service.workspace.create_company("Synthetic PG durable company")["company_id"]
    service.workspace.save_context(cid, "Synthetic PG preserved context", "")
    document, _ = service.workspace.add_file(cid, "original.txt", b"Durable original bytes")
    row = service.workspace.file_row(cid, document["document_id"])
    service.workspace.file_path(row).unlink()
    assert service.workspace.file_path(row).read_bytes() == b"Durable original bytes"
    with pytest.raises(RuntimeError):
        with service.store.connection() as db:
            db.execute("UPDATE company_context SET text=? WHERE company_id=?", ("should roll back", cid))
            raise RuntimeError("injected failure before commit")
    assert service.workspace.context(cid)["description"] == "Synthetic PG preserved context"
    other = Service(
        Settings(
            root=service.settings.root,
            state_dir=tmp_path / "new-ephemeral-disk",
            database_url=service.settings.database_url,
            database_schema=service.settings.database_schema,
        )
    )
    try:
        assert other.workspace.context(cid)["description"] == "Synthetic PG preserved context"
        assert (
            other.workspace.file_path(other.workspace.file_row(cid, document["document_id"])).read_bytes()
            == b"Durable original bytes"
        )
    finally:
        other.corpus.close()
        other.store.close()


def test_pg_parallel_tenants_do_not_mix_context_or_sources(pg_services):
    service = pg_services()

    def create(index):
        principal = create_principal(
            service.workspace, "Synthetic User", "Synthetic company " + str(index), "CFO"
        )
        token = current_principal.set(principal)
        try:
            text = "Private synthetic context " + str(index)
            service.workspace.save_context(principal.company_id, text, "")
            snap = service.workspace.snapshot(principal.company_id, service.corpus, persist=True)
            assert snap["context"]["description"] == text
            assert len(service.workspace.list_custom()) == 1
            return principal.company_id
        finally:
            current_principal.reset(token)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(20)))
    assert len(set(ids)) == 20


def test_pg_backup_can_restore_into_another_empty_schema(pg_services, tmp_path):
    source, target = pg_services(), pg_services()
    principal = source.accounts.register(
        "pg-backup-account", "synthetic-private-password", "Synthetic User", "Synthetic PG Co", "CFO"
    )
    source.workspace.save_context(principal.company_id, "Synthetic backup context", "")
    document, _ = source.workspace.add_file(principal.company_id, "original.txt", b"Synthetic exact bytes")
    path = tmp_path / "private-pg-backup.gz"
    result = backup(source.store, path)
    assert restore_into_empty(target.store, path)["restored_rows"] == result["rows"]
    assert (
        target.accounts.login("pg-backup-account", "synthetic-private-password").company_id
        == principal.company_id
    )
    assert (
        target.workspace.file_path(
            target.workspace.file_row(principal.company_id, document["document_id"])
        ).read_bytes()
        == b"Synthetic exact bytes"
    )


def test_pg_worker_leadership_is_exclusive_across_process_connections(pg_services):
    service = pg_services()
    with service.store.pool.connection() as a, service.store.pool.connection() as b:
        assert a.execute("SELECT pg_try_advisory_lock(12345001)").fetchone()[0]
        a.commit()
        assert not b.execute("SELECT pg_try_advisory_lock(12345001)").fetchone()[0]
        b.commit()
        assert a.execute("SELECT pg_advisory_unlock(12345001)").fetchone()[0]
        a.commit()


def test_pg_authenticated_workspace_startup_endpoints(pg_services, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from alfa_agent.api import create_app

    service = pg_services()
    monkeypatch.setenv("ALFA_AUTH_MODE", "accounts")
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", "synthetic-private-invitation")
    with TestClient(create_app(service.settings)) as client:
        response = client.post(
            "/auth/register",
            data={
                "login": "pg-http-account",
                "password": "synthetic-private-password",
                "full_name": "Synthetic User",
                "company_name": "Synthetic Company",
                "job_title": "CFO",
                "invite_password": "synthetic-private-invitation",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        root = client.get("/")
        assert root.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        client.headers["x-app-token"] = re.search(r'name="app-token" content="([^"]+)"', root.text)[1]
        cid = client.get("/api/me").json()["company_id"]
        routes = [
            "/api/companies",
            "/api/workspace",
            "/api/library",
            "/api/operations",
            "/api/companies/" + cid + "/profile",
            "/api/companies/" + cid + "/documents",
            "/api/companies/" + cid + "/favorites",
            "/api/runs?company_id=" + cid,
            "/api/research?company_id=" + cid,
            "/api/companies/" + cid + "/context/dialogue",
        ]
        for route in routes:
            assert client.get(route).status_code == 200, route


def test_pg_concurrent_idempotency_and_admission_do_not_exhaust_pool(pg_services):
    from alfa_agent.research import ResearchRequest
    from alfa_agent.storage import Conflict

    service = pg_services()
    cid = service.workspace.create_company("Synthetic concurrent company")["company_id"]
    request = ResearchRequest(
        company_id=cid, question="Какие условия финансирования нужно проверить компании?", public_search=False
    )
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(
            pool.map(lambda _: service.research.create(request, "same-idempotency-key")["run_id"], range(10))
        )
    assert len(set(ids)) == 1

    def create(index):
        try:
            return service.research.create(request, "different-key-" + str(index))["run_id"]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        created = list(pool.map(create, range(10)))
    assert sum(x is not None for x in created) == 2
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_jobs WHERE status='QUEUED'").fetchone()[0] == 3

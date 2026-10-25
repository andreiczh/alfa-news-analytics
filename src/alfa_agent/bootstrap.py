"""Install the reviewed initial state once; never overwrite an existing workspace."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3


def prepare_state(settings):
    target = settings.state_path
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    seed = settings.root / "deploy/seed"
    manifest = seed / "manifest.json"
    if not manifest.is_file():
        return
    with (target / "bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if settings.db_path.exists():
            return
        files = json.loads(manifest.read_text())["files"]
        for relative, digest in files.items():
            path = Path(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or relative
                not in {
                    "agent.sqlite3",
                    *[str(Path("uploads") / p.name) for p in (seed / "uploads").glob("*")],
                }
            ):
                raise ValueError("invalid_seed_path")
            if hashlib.sha256((seed / path).read_bytes()).hexdigest() != digest:
                raise ValueError("seed_integrity_error")
        # Publish the database last so a failed first boot can safely be retried.
        for relative in sorted(files, key=lambda name: name == "agent.sqlite3"):
            source, destination = seed / relative, target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(destination.name + ".bootstrap")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            os.replace(temporary, destination)


def seed_postgres_examples(service):
    """Carry only the two explicitly public packaged examples into a new runtime.

    This does not claim to migrate a previous Free container's temporary user data.
    Private actor profiles, sessions, budget counters and old failed runs are excluded.
    """
    if not service.store.pool:
        return
    from .identity import PUBLIC_DEMO_RUNS, PUBLIC_DEMO_COMPANIES

    seed = service.settings.root / "deploy/seed"
    if not (seed / "manifest.json").is_file():
        return
    files = json.loads((seed / "manifest.json").read_text())["files"]
    for relative, expected in files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid_seed_path")
        if hashlib.sha256((seed / path).read_bytes()).hexdigest() != expected:
            raise ValueError("seed_integrity_error")
    with sqlite3.connect((seed / "agent.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        with service.store.connection() as target:
            target.execute("BEGIN IMMEDIATE")
            if target.execute("SELECT 1 FROM meta WHERE key='packaged_public_seed_v1'").fetchone():
                return
            run_ids = sorted(PUBLIC_DEMO_RUNS)
            company_ids = sorted(PUBLIC_DEMO_COMPANIES)
            snapshots = []
            for table in (
                "custom_companies",
                "company_versions",
                "company_files",
                "company_context",
                "company_strategy",
                "company_snapshots",
                "document_jobs",
                "runs",
                "audit",
                "checkpoints",
                "answers",
            ):
                if not source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                if table == "runs":
                    rows = source.execute("SELECT * FROM runs WHERE id IN (?,?)", run_ids).fetchall()
                    snapshots = [json.loads(r["input_json"]).get("profile_snapshot_id") for r in rows]
                elif table in {"audit", "checkpoints", "answers"}:
                    rows = source.execute(
                        "SELECT * FROM "
                        + table
                        + " WHERE run_id IN (?,?)"
                        + (" ORDER BY seq" if table == "audit" else ""),
                        run_ids,
                    ).fetchall()
                elif table == "custom_companies":
                    rows = source.execute("SELECT * FROM custom_companies WHERE id=?", company_ids).fetchall()
                else:
                    rows = source.execute(
                        "SELECT * FROM " + table + " WHERE company_id=?", company_ids
                    ).fetchall()
                for row in rows:
                    columns = [k for k in row.keys() if not (table == "audit" and k == "seq")]
                    target.execute(
                        "INSERT INTO "
                        + table
                        + "("
                        + ",".join(columns)
                        + ") VALUES("
                        + ",".join("?" for _ in columns)
                        + ") ON CONFLICT DO NOTHING",
                        [row[k] for k in columns],
                    )
                    if table == "company_files":
                        content = (seed / "uploads" / row["stored_name"]).read_bytes()
                        if hashlib.sha256(content).hexdigest() != row["sha256"]:
                            raise ValueError("seed_file_integrity_error")
                        target.execute(
                            "INSERT INTO company_file_blobs VALUES(?,?,?) ON CONFLICT DO NOTHING",
                            (row["id"], content, row["sha256"]),
                        )
            for snapshot_id in filter(None, snapshots):
                row = source.execute("SELECT * FROM company_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if row:
                    columns = list(row.keys())
                    target.execute(
                        "INSERT INTO company_snapshots("
                        + ",".join(columns)
                        + ") VALUES("
                        + ",".join("?" for _ in columns)
                        + ") ON CONFLICT DO NOTHING",
                        [row[k] for k in columns],
                    )
            target.execute("INSERT INTO meta VALUES('packaged_public_seed_v1','installed')")

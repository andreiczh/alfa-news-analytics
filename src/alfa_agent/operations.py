"""Readiness, bounded metrics and portable, integrity-checked operator backups."""

from __future__ import annotations

import base64
from collections import Counter, deque
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

from .settings import utcnow
from .storage import Conflict


class Operations:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_status(id TEXT PRIMARY KEY,last_seen REAL NOT NULL,owner TEXT NOT NULL)"
            )

    def heartbeat(self, owner):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO worker_status VALUES('paid-worker',?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,owner=excluded.owner",
                (time.time(), owner),
            )

    def readiness(self, require_worker=False):
        try:
            with self.store.connection() as db:
                db.execute("SELECT 1").fetchone()
                worker = db.execute("SELECT last_seen FROM worker_status WHERE id='paid-worker'").fetchone()
            age = round(max(0, time.time() - worker[0]), 2) if worker else None
            ready = not require_worker or (age is not None and age < 30)
            return {
                "ready": ready,
                "database": "ok",
                "worker_age_seconds": age,
                "worker": "ok" if age is not None and age < 30 else "not_ready",
            }
        except Exception:
            return {"ready": False, "database": "unavailable", "worker": "unknown"}


class HttpMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = Counter()
        self.durations = deque(maxlen=2000)
        self.started = time.monotonic()

    def observe(self, method, route, status, duration):
        with self.lock:
            self.requests[(method, route, status)] += 1
            self.durations.append(duration)

    def snapshot(self):
        with self.lock:
            samples = sorted(self.durations)
            return {
                "uptime_seconds": round(time.monotonic() - self.started, 2),
                "http_requests": sum(self.requests.values()),
                "http_5xx": sum(n for (_, _, status), n in self.requests.items() if status >= 500),
                "sample_count": len(samples),
                "latency_p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))] * 1000, 2)
                if samples
                else None,
            }


def _tables(store, db):
    if store.backend == "postgresql":
        return [
            r[0]
            for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name"
            )
        ]
    return [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _identifier(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ValueError("invalid_backup_identifier")
    return '"' + value + '"'


def _encode(value):
    return (
        {"binary_base64": base64.b64encode(bytes(value)).decode()}
        if isinstance(value, (bytes, bytearray, memoryview))
        else value
    )


def backup(store, path: Path):
    """Operator-only: contains private data and password hashes; never place in Git."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest, rows = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:

            def write(value, signed=True):
                line = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if signed:
                    digest.update(line)
                out.write(line)

            write(
                {
                    "format": "alfa-runtime-backup-v1",
                    "created_at": utcnow().isoformat(),
                    "backend": store.backend,
                }
            )
            with store.connection() as db:
                if store.backend == "postgresql":
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                else:
                    db.execute("BEGIN")
                for table in _tables(store, db):
                    if table == "worker_status":
                        continue
                    cursor = db.execute("SELECT * FROM " + _identifier(table))
                    while batch := cursor.fetchmany(100):
                        for row in batch:
                            write({"table": table, "row": {k: _encode(v) for k, v in dict(row).items()}})
                            rows += 1
            write({"end": True, "sha256": digest.hexdigest(), "rows": rows}, signed=False)
        return {"rows": rows, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def restore_into_empty(store, path: Path):
    """Verify before writing. Never replace a nonempty runtime or replay an uncertain call."""
    digest, records, footer, total = hashlib.sha256(), [], None, 0
    with gzip.open(path, "rb") as file:
        header = file.readline(65536)
        if json.loads(header).get("format") != "alfa-runtime-backup-v1":
            raise ValueError("unknown_backup_format")
        digest.update(header)
        while line := file.readline(32 * 1024 * 1024):
            if not line.endswith(b"\n"):
                raise ValueError("backup_line_limit")
            total += len(line)
            if total > 512 * 1024 * 1024 or len(records) > 500_000:
                raise ValueError("backup_size_limit")
            value = json.loads(line)
            if value.get("end"):
                footer = value
                if file.read(1):
                    raise ValueError("backup_trailing_data")
                break
            digest.update(line)
            records.append(value)
    if not footer or footer["sha256"] != digest.hexdigest() or footer["rows"] != len(records):
        raise ValueError("backup_integrity_failed")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        tables = set(_tables(store, db))
        for table in tables - {"worker_status"}:
            if db.execute("SELECT 1 FROM " + _identifier(table) + " LIMIT 1").fetchone():
                raise Conflict("restore_requires_empty_runtime")
        worker = (
            db.execute("SELECT last_seen FROM worker_status WHERE id='paid-worker'").fetchone()
            if "worker_status" in tables
            else None
        )
        if worker and worker[0] > time.time() - 30:
            raise Conflict("stop_worker_before_restore")
        for record in records:
            table, row = record["table"], record["row"]
            if table not in tables:
                raise ValueError("backup_table_not_in_schema")
            names = list(row)
            values = [
                base64.b64decode(row[k]["binary_base64"], validate=True)
                if isinstance(row[k], dict) and set(row[k]) == {"binary_base64"}
                else row[k]
                for k in names
            ]
            db.execute(
                "INSERT INTO "
                + _identifier(table)
                + "("
                + ",".join(_identifier(k) for k in names)
                + ") VALUES("
                + ",".join("?" for _ in names)
                + ")",
                values,
            )
        for table in ("runs", "research_jobs", "context_turns", "document_jobs"):
            if table in tables:
                # An interrupted paid call is never automatically charged again.
                if table in {"runs", "research_jobs"}:
                    db.execute(
                        "UPDATE "
                        + _identifier(table)
                        + " SET status='FAILED',lease_until=NULL WHERE status='RUNNING'"
                    )
                else:
                    db.execute(
                        "UPDATE "
                        + _identifier(table)
                        + " SET status='FAILED',lease_until=NULL,error_code='interrupted_manual_retry' WHERE status='RUNNING'"
                    )
        if "account_sessions" in tables:
            db.execute("DELETE FROM account_sessions")
        for table in ("runs", "research_jobs"):
            if table in tables:
                for row in db.execute(
                    "SELECT id,state_json FROM " + _identifier(table) + " WHERE status='FAILED'"
                ).fetchall():
                    state = json.loads(row["state_json"])
                    if state.get("status") == "RUNNING" or (
                        table != "runs" and state.get("phase") not in {"FINISHED", "FAILED"}
                    ):
                        if table == "runs":
                            state.update(status="FAILED", last_error_code="interrupted_manual_retry")
                        else:
                            state.update(phase="FINISHED", error_code="interrupted_manual_retry")
                        db.execute(
                            "UPDATE " + _identifier(table) + " SET state_json=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False), row["id"]),
                        )
        if store.backend == "postgresql" and "audit" in tables:
            db.execute(
                "SELECT setval(pg_get_serial_sequence('audit','seq'),COALESCE((SELECT MAX(seq) FROM audit),0)+1,false)"
            )
    return {
        "restored_rows": len(records),
        "sha256": digest.hexdigest(),
        "uncertain_paid_calls": "require_manual_retry",
    }

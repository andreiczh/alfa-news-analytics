from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .settings import utcnow


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:20]


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Store:
    """Short SQLite transactions only. Never hold a transaction over HTTP."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, company TEXT NOT NULL,
              idem TEXT NOT NULL, payload_hash TEXT NOT NULL, input_json TEXT NOT NULL,
              state_json TEXT NOT NULL, result_json TEXT, version INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL, lease_until REAL, created_at TEXT NOT NULL,
              UNIQUE(tenant,idem));
            CREATE INDEX IF NOT EXISTS runs_queue ON runs(status,created_at);
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              at TEXT NOT NULL, node TEXT NOT NULL, event TEXT NOT NULL, data_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
              started_at TEXT NOT NULL, status TEXT NOT NULL, usage_json TEXT,
              request_hash TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS answers (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, at TEXT NOT NULL, data_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (
              run_id TEXT NOT NULL, version INTEGER NOT NULL, at TEXT NOT NULL,
              input_json TEXT NOT NULL, state_json TEXT NOT NULL,
              PRIMARY KEY(run_id,version));
            CREATE TABLE IF NOT EXISTS document_jobs (
              id TEXT PRIMARY KEY,company_id TEXT NOT NULL,document_id TEXT NOT NULL,
              status TEXT NOT NULL,phase TEXT NOT NULL,created_at TEXT NOT NULL,
              lease_until REAL,next_eligible_at TEXT,error_code TEXT,
              progress_json TEXT NOT NULL DEFAULT '{}');
            PRAGMA user_version=2;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, tenant, company, idem, payload, state):
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        identity = {
            key: value for key, value in payload.items() if key not in {"as_of", "profile_snapshot_id"}
        }
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM runs WHERE tenant=? AND idem=?", (tenant, idem)).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise Conflict("idempotency_payload_mismatch")
                return old["id"], False
            run_id = state["run_id"]
            db.execute(
                "INSERT INTO runs(id,tenant,company,idem,payload_hash,input_json,state_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    tenant,
                    company,
                    idem,
                    digest,
                    raw,
                    json.dumps(state, ensure_ascii=False),
                    "QUEUED",
                    utcnow().isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                (run_id, 1, utcnow().isoformat(), raw, json.dumps(state, ensure_ascii=False)),
            )
            return run_id, True

    @staticmethod
    def decode(row):
        if not row:
            raise NotFound("run_not_found")
        result = dict(row)
        for key in ("input_json", "state_json", "result_json"):
            raw = result.pop(key)
            result[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return result

    def get(self, run_id, tenant="demo"):
        with self.connection() as db:
            return self.decode(
                db.execute("SELECT * FROM runs WHERE id=? AND tenant=?", (run_id, tenant)).fetchone()
            )

    def save(self, row, state, result=None, final_audit=None):
        with self.connection() as db:
            cur = db.execute(
                "UPDATE runs SET state_json=?,status=?,result_json=COALESCE(?,result_json),version=version+1 WHERE id=? AND version=?",
                (
                    json.dumps(state, ensure_ascii=False),
                    state["status"],
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    row["id"],
                    row["version"],
                ),
            )
            if cur.rowcount != 1:
                raise Conflict("state_changed")
            db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    row["version"] + 1,
                    utcnow().isoformat(),
                    json.dumps(row["input"], ensure_ascii=False),
                    json.dumps(state, ensure_ascii=False),
                ),
            )
            db.execute(
                "UPDATE attempts SET status='consumed' WHERE run_id=? AND status='received'", (row["id"],)
            )
            if final_audit:
                db.execute(
                    "INSERT INTO audit(run_id,at,node,event,data_json) VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        utcnow().isoformat(),
                        "PUBLISH",
                        "published",
                        json.dumps(final_audit, ensure_ascii=False),
                    ),
                )
        return self.get(row["id"], row["tenant"])

    def audit(self, run_id, node, event, data):
        with self.connection() as db:
            db.execute(
                "INSERT INTO audit(run_id,at,node,event,data_json) VALUES(?,?,?,?,?)",
                (run_id, utcnow().isoformat(), node, event, json.dumps(data, ensure_ascii=False)),
            )

    def trace(self, run_id, tenant="demo"):
        self.get(run_id, tenant)
        with self.connection() as db:
            return [
                {
                    "seq": row["seq"],
                    "at": row["at"],
                    "node": row["node"],
                    "event": row["event"],
                    "data": json.loads(row["data_json"]),
                }
                for row in db.execute("SELECT * FROM audit WHERE run_id=? ORDER BY seq", (run_id,))
            ]

    def claim(self, lease_seconds=90, target_run_id=None):
        now = utcnow().timestamp()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A lost HTTP response can have been billed. Never silently replay it.
            expired = db.execute(
                "SELECT * FROM runs WHERE status='RUNNING' AND lease_until<?", (now,)
            ).fetchall()
            for old in expired:
                ambiguous = db.execute(
                    "SELECT 1 FROM attempts WHERE run_id=? AND status IN ('started','received')", (old["id"],)
                ).fetchone()
                state = json.loads(old["state_json"])
                state["status"] = "FAILED" if ambiguous else "QUEUED"
                if ambiguous:
                    state.update(node="FINISHED", last_error_code="external_result_unknown")
                db.execute(
                    "UPDATE runs SET status=?,state_json=?,version=version+1 WHERE id=?",
                    (state["status"], json.dumps(state, ensure_ascii=False), old["id"]),
                )
            deferred = db.execute("SELECT * FROM runs WHERE status='DEFERRED_BUDGET'").fetchall()
            for old in deferred:
                state = json.loads(old["state_json"])
                from datetime import datetime

                eligible = state.get("next_eligible_at")
                if eligible and datetime.fromisoformat(eligible) <= utcnow():
                    state.update(status="QUEUED", node="HYDRATE")
                    db.execute(
                        "UPDATE runs SET status='QUEUED',state_json=?,version=version+1 WHERE id=?",
                        (json.dumps(state, ensure_ascii=False), old["id"]),
                    )
            if target_run_id:
                row = db.execute(
                    "SELECT * FROM runs WHERE status='QUEUED' AND id=?", (target_run_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM runs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
                ).fetchone()
            db.execute(
                "UPDATE document_jobs SET status='FAILED',error_code='interrupted_manual_retry' WHERE status='RUNNING' AND lease_until<?",
                (now,),
            )
            if (
                db.execute("SELECT 1 FROM runs WHERE status='RUNNING' LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM document_jobs WHERE status='RUNNING' LIMIT 1").fetchone()
            ):
                return None
            if not row:
                return None
            state = json.loads(row["state_json"])
            state["status"] = "RUNNING"
            state["state_version"] = row["version"] + 1
            db.execute(
                "UPDATE runs SET status='RUNNING',state_json=?,lease_until=?,version=version+1 WHERE id=?",
                (json.dumps(state, ensure_ascii=False), now + lease_seconds, row["id"]),
            )
            db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    row["version"] + 1,
                    utcnow().isoformat(),
                    row["input_json"],
                    json.dumps(state, ensure_ascii=False),
                ),
            )
            return self.decode(db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())

    def renew(self, run_id, seconds):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET lease_until=? WHERE id=? AND status='RUNNING'",
                (utcnow().timestamp() + seconds, run_id),
            )

    def attempt_start(self, run_id, stage, request_hash, model):
        attempt_id = new_id("llm")
        with self.connection() as db:
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, run_id, stage, utcnow().isoformat(), "started", None, request_hash, model),
            )
        return attempt_id

    def attempt_end(self, attempt_id, status, usage=None):
        with self.connection() as db:
            db.execute(
                "UPDATE attempts SET status=?,usage_json=COALESCE(?,usage_json) WHERE id=?",
                (status, json.dumps(usage) if usage else None, attempt_id),
            )

    def attempts(self, run_id):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,stage,status,usage_json,model FROM attempts WHERE run_id=?", (run_id,)
                )
            ]

    def meta(self, key, value=None):
        with self.connection() as db:
            if value is not None:
                db.execute(
                    "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                return value
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def cancel(self, run_id, tenant="demo"):
        row = self.get(run_id, tenant)
        if row["status"] in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
            return row
        state = row["state"]
        state.update(status="CANCELLED", node="FINISHED")
        return self.save(row, state)

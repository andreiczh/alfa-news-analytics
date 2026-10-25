"""Grounded context interview: model drafts, user reviews, host commits a version."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gateway import GatewayError
from .corpus import AccessDenied
from .personalization import marked_test_context
from .settings import utcnow
from .storage import Conflict, NotFound, new_id

CATEGORY_LABELS = {
    "general": "Общая информация",
    "strategy": "Стратегия и развитие",
    "finance": "Продажи и финансы",
    "risks": "Риски и ограничения",
    "priorities": "Что отслеживать",
    "other": "Дополнительный контекст",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextField(Strict):
    key: str = Field(pattern=r"^[a-z][a-z_0-9]{1,49}$")
    category: Literal["general", "strategy", "finance", "risks", "priorities", "other"]
    label: str = Field(min_length=2, max_length=80)
    value: str = Field(max_length=1800)


class ProposedField(ContextField):
    message_id: str = Field(max_length=100)
    quote: str = Field(min_length=2, max_length=1800)


class ContextProposal(Strict):
    response: str = Field(min_length=10, max_length=900)
    fields: list[ProposedField] = Field(default_factory=list, max_length=12)
    questions: list[str] = Field(min_length=1, max_length=3)


class ContextMessage(Strict):
    message: str = Field(default="", max_length=5000)
    expected_revision: int = Field(ge=0)


class ContextConfirm(Strict):
    expected_revision: int = Field(ge=0)
    fields: list[ContextField] = Field(min_length=1, max_length=30)


def norm(text):
    return " ".join(text.split()).casefold()


def proposal_issues(proposal, messages):
    users = {m["id"]: m["text"] for m in messages if m["role"] == "user"}
    issues, keys = [], set()
    for item in proposal.fields:
        if item.key in keys:
            issues.append("duplicate_context_field")
        keys.add(item.key)
        if item.message_id not in users or norm(item.quote) not in norm(users.get(item.message_id, "")):
            issues.append("context_quote_not_in_user_message")
        if norm(item.value) != norm(item.quote):
            issues.append("context_value_must_preserve_user_words")
    if any(len(q) > 350 for q in proposal.questions):
        issues.append("context_question_too_long")
    return list(dict.fromkeys(issues))


class ContextService:
    def __init__(self, service):
        self.service, self.store, self.settings = service, service.store, service.settings
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS context_turns(
                    id TEXT PRIMARY KEY,tenant TEXT NOT NULL,company_id TEXT NOT NULL,idem TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,input_json TEXT NOT NULL,result_json TEXT,status TEXT NOT NULL,
                    created_at TEXT NOT NULL,error_code TEXT,retry_at TEXT,lease_until REAL,confirm_hash TEXT,
                    UNIQUE(tenant,idem));
                CREATE INDEX IF NOT EXISTS context_queue ON context_turns(status,created_at);
            """)

    def require_custom(self, company_id):
        self.service.authorize_company(company_id)
        self.service.workspace.require_writable(company_id)
        if not self.service.workspace.custom_exists(company_id):
            raise AccessDenied("create_own_company_first")

    def _row(self, turn_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM context_turns WHERE id=? AND tenant=?", (turn_id, self.service.tenant)
            ).fetchone()
        if not row:
            raise NotFound("context_turn_not_found")
        self.service.authorize_company(row["company_id"])
        return dict(row)

    def history(self, company_id):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM context_turns WHERE tenant=? AND company_id=? ORDER BY created_at DESC LIMIT 16",
                (self.service.tenant, company_id),
            ).fetchall()
        return {
            "context": self.service.workspace.context(company_id),
            "categories": CATEGORY_LABELS,
            "turns": [self.public(dict(r)) for r in reversed(rows)],
        }

    @staticmethod
    def public(row):
        request = json.loads(row["input_json"])
        return {
            "turn_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "message": request["message"],
            "role": request["role"],
            "base_revision": request["base_revision"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_code": row["error_code"],
            "retry_at": row["retry_at"],
        }

    def create(self, company_id, payload, idem):
        self.require_custom(company_id)
        digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        with self.store.connection() as db:
            old = db.execute(
                "SELECT * FROM context_turns WHERE tenant=? AND idem=?", (self.service.tenant, idem)
            ).fetchone()
            if old:
                if old["payload_hash"] != digest or old["company_id"] != company_id:
                    raise Conflict("idempotency_payload_mismatch")
                return self.public(dict(old))
        context = self.service.workspace.context(company_id)
        if context["revision"] != payload.expected_revision:
            raise Conflict("company_context_changed")
        previous = self.history(company_id)["turns"]
        messages = []
        for turn in previous[-6:]:
            if turn["message"]:
                messages.append({"id": turn["turn_id"], "role": "user", "text": turn["message"]})
            if turn["result"]:
                messages.append(
                    {
                        "id": turn["turn_id"] + "_assistant",
                        "role": "assistant",
                        "text": turn["result"]["response"] + "\n" + "\n".join(turn["result"]["questions"]),
                    }
                )
        tid = new_id("context")
        if payload.message.strip():
            messages.append({"id": tid, "role": "user", "text": payload.message.strip()})
        while sum(len(m["text"]) for m in messages) > 12000 and len(messages) > 1:
            messages.pop(0)
        prefs = self.service.personalization.get()
        snapshot = self.service.workspace.snapshot(company_id, self.service.corpus)
        request = {
            "message": payload.message.strip(),
            "messages": messages,
            "base_revision": context["revision"],
            "role": prefs["role"],
            "job_title": prefs["job_title"],
            "saved_context": context,
            "known_features": [
                {
                    "name": f["name"],
                    "status": f["status"],
                    "text_value": (f.get("text_value") or "")[:400],
                    "numeric_value": f.get("numeric_value"),
                }
                for f in [
                    f
                    for f in snapshot["profile"]["features"]
                    if f["status"] not in {"missing", "conflicting"}
                ][:12]
            ],
        }
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT count(*) FROM context_turns WHERE tenant=? AND company_id=? AND status IN ('QUEUED','RUNNING','DEFERRED_BUDGET')",
                (self.service.tenant, company_id),
            ).fetchone()[0]:
                raise Conflict("context_dialogue_busy")
            if (
                db.execute(
                    "SELECT count(*) FROM context_turns WHERE status IN ('QUEUED','RUNNING')"
                ).fetchone()[0]
                >= 20
            ):
                raise Conflict("context_queue_limit")
            db.execute(
                "INSERT INTO context_turns VALUES(?,?,?,?,?,?,NULL,'QUEUED',?,NULL,NULL,NULL,NULL)",
                (
                    tid,
                    self.service.tenant,
                    company_id,
                    idem,
                    digest,
                    json.dumps(request, ensure_ascii=False),
                    utcnow().isoformat(),
                ),
            )
        return self.public(self._row(tid))

    def confirm(self, company_id, turn_id, payload):
        row = self._row(turn_id)
        if row["company_id"] != company_id:
            raise NotFound("context_turn_not_found")
        self.require_custom(company_id)
        digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        if row["status"] == "CONFIRMED" and row["confirm_hash"] == digest:
            return self.service.workspace.context(company_id)
        if row["status"] != "SUCCEEDED":
            raise Conflict("context_proposal_not_ready")
        request = json.loads(row["input_json"])
        if request["base_revision"] != payload.expected_revision:
            raise Conflict("company_context_changed")
        if len({f.key for f in payload.fields}) != len(payload.fields):
            raise Conflict("duplicate_context_field")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT id FROM context_turns WHERE tenant=? AND company_id=? ORDER BY created_at DESC LIMIT 1",
                (self.service.tenant, company_id),
            ).fetchone()
            strategy = db.execute(
                "SELECT * FROM company_strategy WHERE company_id=?", (company_id,)
            ).fetchone()
            revision = strategy["revision"] if strategy else 0
            if not latest or latest[0] != turn_id or revision != payload.expected_revision:
                raise Conflict("company_context_changed")
            fields = json.loads(strategy["fields_json"]) if strategy else {}
            for item in payload.fields:
                if item.value.strip():
                    fields[item.key] = {
                        **item.model_dump(),
                        "value": item.value.strip(),
                        "synthetic": marked_test_context(request["message"]),
                        "origin": "user_reviewed",
                        "turn_id": turn_id,
                        "confirmed_at": utcnow().isoformat(),
                    }
                else:
                    fields.pop(item.key, None)
            if len(fields) > 40 or sum(len(f["value"]) for f in fields.values()) > 20000:
                raise Conflict("company_context_limit")
            db.execute(
                "INSERT INTO company_strategy VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET revision=excluded.revision,fields_json=excluded.fields_json,updated_at=excluded.updated_at",
                (company_id, revision + 1, json.dumps(fields, ensure_ascii=False), utcnow().isoformat()),
            )
            db.execute("UPDATE company_versions SET version=version+1 WHERE company_id=?", (company_id,))
            db.execute(
                "UPDATE context_turns SET status='CONFIRMED',confirm_hash=? WHERE id=? AND status='SUCCEEDED'",
                (digest, turn_id),
            )
        return self.service.workspace.context(company_id)

    def save_fields(self, company_id, payload):
        self.require_custom(company_id)
        if len({f.key for f in payload.fields}) != len(payload.fields):
            raise Conflict("duplicate_context_field")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM company_strategy WHERE company_id=?", (company_id,)).fetchone()
            revision = row["revision"] if row else 0
            if revision != payload.expected_revision:
                raise Conflict("company_context_changed")
            fields = json.loads(row["fields_json"]) if row else {}
            legacy_turn_ids = {f["turn_id"] for f in fields.values() if f.get("turn_id")}
            if legacy_turn_ids:
                placeholders = ",".join("?" for _ in legacy_turn_ids)
                origins = db.execute(
                    "SELECT id,input_json FROM context_turns WHERE company_id=? AND status='CONFIRMED' "
                    "AND id IN (" + placeholders + ")",
                    [company_id, *legacy_turn_ids],
                ).fetchall()
                test_turns = {
                    origin["id"]
                    for origin in origins
                    if marked_test_context(json.loads(origin["input_json"])["message"])
                }
                for previous in fields.values():
                    if previous.get("turn_id") in test_turns:
                        previous["synthetic"] = True
            for item in payload.fields:
                if item.value.strip():
                    fields[item.key] = {
                        **item.model_dump(),
                        "value": item.value.strip(),
                        "synthetic": fields.get(item.key, {}).get("synthetic", False)
                        or marked_test_context(item.value),
                        "origin": "user_edited",
                        "confirmed_at": utcnow().isoformat(),
                    }
                else:
                    fields.pop(item.key, None)
            if len(fields) > 40 or sum(len(f["value"]) for f in fields.values()) > 20000:
                raise Conflict("company_context_limit")
            db.execute(
                "INSERT INTO company_strategy VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET revision=excluded.revision,fields_json=excluded.fields_json,updated_at=excluded.updated_at",
                (company_id, revision + 1, json.dumps(fields, ensure_ascii=False), utcnow().isoformat()),
            )
            db.execute("UPDATE company_versions SET version=version+1 WHERE company_id=?", (company_id,))
        return self.service.workspace.context(company_id)

    def cancel(self, company_id, turn_id):
        row = self._row(turn_id)
        if row["company_id"] != company_id:
            raise NotFound("context_turn_not_found")
        self.service.workspace.require_writable(company_id)
        with self.store.connection() as db:
            db.execute(
                "UPDATE context_turns SET status='CANCELLED' WHERE id=? AND status IN ('QUEUED','RUNNING','DEFERRED_BUDGET')",
                (turn_id,),
            )
        return self.public(self._row(turn_id))

    def claim(self):
        now = utcnow()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A paid request may already have reached the provider. Require an explicit new turn after interruption.
            db.execute(
                "UPDATE context_turns SET status='FAILED',error_code='context_interrupted_manual_retry' WHERE status='RUNNING' AND lease_until<?",
                (now.timestamp(),),
            )
            row = db.execute(
                "SELECT * FROM context_turns WHERE status='QUEUED' OR (status='DEFERRED_BUDGET' AND retry_at<=?) ORDER BY created_at LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE context_turns SET status='RUNNING',lease_until=? WHERE id=?",
                (now.timestamp() + 180, row["id"]),
            )
            return dict(row)


class ContextEngine:
    def __init__(self, context, gateway):
        self.context, self.gateway = context, gateway

    async def once(self):
        row = self.context.claim()
        if not row:
            return None
        request = json.loads(row["input_json"])
        try:
            repair = None
            result = None
            for _ in range(2):
                raw = await self.gateway.complete(
                    row["id"],
                    "context_interview",
                    request,
                    ContextProposal.model_json_schema(),
                    repair=repair,
                    model=self.context.settings.research_model,
                )
                try:
                    proposal = ContextProposal.model_validate_json(raw)
                    issues = proposal_issues(proposal, request["messages"])
                    if issues:
                        raise ValueError(";".join(issues))
                    result = proposal.model_dump(mode="json")
                    break
                except (ValidationError, ValueError) as error:
                    repair = {"raw": raw, "errors": [{"message": str(error)[:1200]}]}
            if result is None:
                raise GatewayError("context_contract_invalid")
            with self.context.store.connection() as db:
                db.execute(
                    "UPDATE context_turns SET status='SUCCEEDED',result_json=?,lease_until=NULL WHERE id=? AND status='RUNNING'",
                    (json.dumps(result, ensure_ascii=False), row["id"]),
                )
            self.context.store.audit(
                row["id"],
                "CONTEXT_REVIEW",
                "proposal_prepared",
                {"fields": [f["key"] for f in result["fields"]], "requires_confirmation": True},
            )
        except GatewayError as error:
            with self.context.store.connection() as db:
                db.execute(
                    "UPDATE context_turns SET status=?,error_code=?,retry_at=?,lease_until=NULL WHERE id=? AND status='RUNNING'",
                    (
                        "DEFERRED_BUDGET" if error.code == "shared_budget_exhausted" else "FAILED",
                        error.code,
                        error.retry_at,
                        row["id"],
                    ),
                )
        except Exception as error:
            with self.context.store.connection() as db:
                db.execute(
                    "UPDATE context_turns SET status='FAILED',error_code='context_internal_error',lease_until=NULL WHERE id=? AND status='RUNNING'",
                    (row["id"],),
                )
            self.context.store.audit(row["id"], "CONTEXT", "failed", {"exception_type": type(error).__name__})
        return row["id"]

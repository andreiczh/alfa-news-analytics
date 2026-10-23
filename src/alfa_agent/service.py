from __future__ import annotations

from decimal import Decimal
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .corpus import AccessDenied, Corpus
from .domain import (
    Citation,
    DocumentRef,
    Evidence,
    FeatureCandidate,
    Node,
    NumericValue,
    RunStatus,
    TimeScope,
)
from .engine import initial_state
from .settings import Settings, utcnow
from .state import RuntimeState
from .storage import Conflict, Store, new_id
from .workspace import Workspace


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str = Field(default="demo_floating", max_length=100)
    scenario_delta_pp: Decimal = Field(default=Decimal("-1"), ge=-5, le=5, allow_inf_nan=False)
    horizon_days: int = Field(default=90, ge=1, le=366)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    question_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    value_rub: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000000000"), allow_inf_nan=False)
    skip: bool = False

    @model_validator(mode="after")
    def exactly_one_answer(self):
        if self.skip == (self.value_rub is not None):
            raise ValueError("provide value_rub or skip, not both")
        return self


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.corpus = Corpus(settings.bundle_path)
        self.workspace = Workspace(settings, self.store)
        self.corpus.workspace = self.workspace
        # Explicit local demo principal, not Alfa SSO or production authorization.
        self.allowed_companies = frozenset(self.corpus.profiles)

    def authorize_company(self, company_id):
        if (
            company_id not in self.allowed_companies
            and not self.workspace.case.exists(company_id)
            and not self.workspace.custom_exists(company_id)
        ):
            raise AccessDenied("company_not_accessible")

    def create(self, payload: CreateRun, idem):
        self.authorize_company(payload.company_id)
        request = payload.model_dump(mode="json")
        with self.store.connection() as db:
            old = db.execute(
                "SELECT id,input_json FROM runs WHERE tenant='demo' AND idem=?", (idem,)
            ).fetchone()
        if old:
            previous = json.loads(old["input_json"])
            if any(previous.get(k) != v for k, v in request.items()):
                raise Conflict("idempotency_payload_mismatch")
            return self.get(old["id"])
        files = self.workspace.files(payload.company_id)
        if any(f["status"] in {"QUEUED", "RUNNING", "DEFERRED_BUDGET"} for f in files):
            raise Conflict("company_documents_processing")
        snapshot = self.workspace.snapshot(payload.company_id, self.corpus, persist=True)
        request["profile_snapshot_id"] = snapshot["snapshot_id"]
        request["as_of"] = utcnow().isoformat()
        state = initial_state(new_id("run"))
        run_id, created = self.store.create(
            "demo", payload.company_id, idem, request, state.model_dump(mode="json")
        )
        if created:
            self.store.audit(
                run_id,
                "HYDRATE",
                "queued",
                {"company_id": payload.company_id, "event_id": "rate_decision_20260911"},
            )
        return self.get(run_id)

    def get(self, run_id):
        row = self.store.get(run_id)
        self.authorize_company(row["company"])
        self.corpus.load_run_snapshot(row["input"])
        state = RuntimeState.model_validate(row["state"])
        if row["result"] and row["input"].get("profile_snapshot_id"):
            source = row["result"].get("company_context", {}).get("source", "unknown")
            row["result"]["corpus_metadata"] = {
                **row["result"].get("corpus_metadata", {}),
                "scope": "News/bank corpus metadata. Active company provenance is in company_context; original built-in demo profiles are synthetic.",
                "company_profiles": "Active company source: "
                + source
                + ". Built-in legacy demo profiles: synthetic.",
            }
        return {
            "run_id": run_id,
            "version": row["version"],
            "status": row["status"],
            "node": state.node.value,
            "company_id": row["company"],
            "llm_calls": state.llm_calls,
            "tool_calls": state.tool_calls,
            "replans": state.replans,
            "active_elapsed_ms": state.active_elapsed_ms,
            "question": state.question.model_dump(mode="json") if state.question else None,
            "question_id": state.pending_question_id,
            "error_code": state.last_error_code,
            "next_eligible_at": state.next_eligible_at.isoformat() if state.next_eligible_at else None,
            "result": row["result"],
            "trace": self.store.trace(run_id),
        }

    def answer(self, run_id, answer: Answer):
        self.get(run_id)
        digest = hashlib.sha256(answer.model_dump_json(exclude={"expected_version"}).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM answers WHERE id=?", (answer.answer_id,)).fetchone()
            if old:
                if old["run_id"] != run_id or old["payload_hash"] != digest:
                    raise Conflict("answer_id_conflict")
                # The same answer is accepted once even if the UI retries later.
            else:
                row = db.execute("SELECT * FROM runs WHERE id=? AND tenant='demo'", (run_id,)).fetchone()
                if not row or row["version"] != answer.expected_version or row["status"] != "WAITING_INPUT":
                    raise Conflict("answer_state_changed")
                state = RuntimeState.model_validate_json(row["state_json"])
                if (
                    state.pending_question_id != answer.question_id
                    or not state.question
                    or state.question.field != "floating_debt_rub"
                ):
                    raise Conflict("question_mismatch")
                request = json.loads(row["input_json"])
                now = utcnow()
                if answer.skip:
                    state.skipped_fields.append("floating_debt_rub")
                else:
                    did, eid = new_id("answer_doc"), new_id("answer_ev")
                    text = f"Клиент сообщил остаток плавающего долга: {answer.value_rub} RUB. Значение не проверено независимо."
                    doc = DocumentRef(
                        document_id=did,
                        source_kind="user_answer",
                        title="Ответ на вопрос о плавающем долге",
                        publisher="Пользователь демо-компании",
                        published_at=now.date(),
                        publication_date_raw=now.isoformat(),
                        available_at=now,
                        sha256=hashlib.sha256(text.encode()).hexdigest(),
                        tenant_company_id=request["company_id"],
                    )
                    evidence = Evidence(
                        evidence_id=eid,
                        document_id=did,
                        object_kind="answer",
                        locator="Ответ на вопрос " + answer.question_id,
                        quote=text,
                        extraction_method="user",
                    )
                    state.answer_documents.append(doc)
                    state.answer_evidence.append(evidence)
                    state.evidence_ids.append(eid)
                    profile = state.profile
                    profile.features = [f for f in profile.features if f.name != "floating_debt_rub"]
                    profile.features.append(
                        FeatureCandidate(
                            feature_id=new_id("debt_feature"),
                            name="floating_debt_rub",
                            numeric_value=NumericValue(
                                value=answer.value_rub, unit="RUB", raw=str(answer.value_rub) + " RUB"
                            ),
                            status="self_reported",
                            period=TimeScope(),
                            source_refs=[Citation(kind="evidence", ref_id=eid)],
                        )
                    )
                    profile.version += 1
                    profile.as_of = now
                    profile.source_snapshot_ids.append(did)
                    state.profile_version = profile.version
                request["as_of"] = now.isoformat()
                state.input_revision += 1
                state.status = RunStatus.QUEUED
                state.node = Node.HYDRATE
                state.question = None
                state.pending_question_id = None
                state.state_version = row["version"] + 1
                db.execute(
                    "INSERT INTO answers VALUES(?,?,?,?,?)",
                    (answer.answer_id, run_id, answer.question_id, digest, now.isoformat()),
                )
                db.execute(
                    "UPDATE runs SET status='QUEUED',state_json=?,input_json=?,version=version+1 WHERE id=? AND version=?",
                    (
                        state.model_dump_json(),
                        json.dumps(request, ensure_ascii=False),
                        run_id,
                        row["version"],
                    ),
                )
                db.execute(
                    "INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        row["version"] + 1,
                        now.isoformat(),
                        json.dumps(request, ensure_ascii=False),
                        state.model_dump_json(),
                    ),
                )
                db.execute(
                    "INSERT INTO audit(run_id,at,node,event,data_json) VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        now.isoformat(),
                        "HYDRATE",
                        "answer_accepted",
                        json.dumps(
                            {
                                "question_id": answer.question_id,
                                "revision": state.input_revision,
                                "skipped": answer.skip,
                            }
                        ),
                    ),
                )
        return self.get(run_id)

"""Domain contracts, initially copied from architecture/2026-10-23/contracts.py.

Python 3.12 + Pydantic 2. All objects supplied by an LLM are untrusted.
Cross-object permissions, source truth, budgets and transitions belong to code.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=1000)]


class ClaimType(StrEnum):
    FACT = "FACT"
    ASSESSMENT = "ASSESSMENT"
    FORECAST = "FORECAST"
    SCENARIO = "SCENARIO"
    RECOMMENDATION = "RECOMMENDATION"


class TimeScope(Contract):
    raw: str | None = None
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.end < self.start:
            raise ValueError("time interval reversed")
        return self


class NumericValue(Contract):
    # Exactly a point OR a range. No conversion of a range to its midpoint.
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    minimum: Decimal | None = Field(default=None, allow_inf_nan=False)
    maximum: Decimal | None = Field(default=None, allow_inf_nan=False)
    unit: Annotated[str, Field(min_length=1, max_length=80)]
    raw: ShortText

    @model_validator(mode="after")
    def point_or_range(self):
        is_range = self.minimum is not None or self.maximum is not None
        if (self.value is not None) == is_range:
            raise ValueError("provide a point or a bounded/one-sided range")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("numeric interval reversed")
        return self


class StatementOwner(Contract):
    kind: Literal["bank", "external", "company", "user", "model", "unknown"]
    name: str | None = None
    raw_attribution: str | None = None


class DocumentRef(Contract):
    document_id: ID
    source_kind: Literal["bank_pdf", "news", "company_upload", "user_answer", "case_csv"]
    title: ShortText
    publisher: str | None
    published_at: date | None
    publication_date_raw: str | None
    available_at: AwareDatetime
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # Server-owned fields. Must not be taken from model output.
    tenant_company_id: ID | None = None
    public_url: str | None = None


class Evidence(Contract):
    evidence_id: ID
    document_id: ID
    page: int | None = Field(default=None, ge=1)
    object_kind: Literal["text", "table", "chart", "csv_row", "answer"]
    locator: ShortText
    quote: str | None = Field(default=None, max_length=2500)
    extraction_method: Literal["text", "table_parser", "vision", "manual", "csv", "user"]

    @model_validator(mode="after")
    def require_text_quote(self):
        if self.object_kind == "text" and not (self.quote and self.quote.strip()):
            raise ValueError("text evidence requires a verbatim quote")
        return self


class SourceClaim(Contract):
    claim_id: ID
    document_id: ID
    text: ShortText
    claim_type: ClaimType
    significance: Literal["PRIMARY", "SUPPORTING"]
    owner: StatementOwner
    modality_raw: str | None
    conditions: list[str] = Field(default_factory=list)
    time_scope: TimeScope
    numbers: list[NumericValue] = Field(default_factory=list)
    evidence_ids: list[ID] = Field(min_length=1)
    # AI output can never assert that a human verified it.
    review_status: Literal["pending_human_review", "requires_clarification"]

    @model_validator(mode="after")
    def no_model_opinion_in_source_claim(self):
        if self.owner.kind == "model":
            raise ValueError("model interpretation does not belong to source extraction")
        return self


class SourceRelation(Contract):
    relation_id: ID
    from_claim_id: ID
    to_claim_id: ID
    kind: Literal["SUPPORTS", "EXPLAINS", "CONTEXT_FOR", "QUALIFIES"]
    evidence_ids: list[ID] = Field(min_length=1)
    rationale_source_only: ShortText


class ExtractionIssue(Contract):
    code: Literal["unreadable", "unknown_date", "ambiguous_owner", "ambiguous_number", "other"]
    page: int | None = Field(default=None, ge=1)
    description: ShortText
    affected_claim_ids: list[ID] = Field(default_factory=list)


class BankExtraction(Contract):
    """One complete PDF assembled from page packets; no claim-count cap."""

    document_id: ID
    pages_processed: list[Annotated[int, Field(ge=1)]]
    evidence: list[Evidence]
    claims: list[SourceClaim]
    relations: list[SourceRelation]
    issues: list[ExtractionIssue]

    @model_validator(mode="after")
    def references_are_local(self):
        claims = {c.claim_id: c for c in self.claims}
        evidence = {e.evidence_id: e for e in self.evidence}
        if len(claims) != len(self.claims) or len(evidence) != len(self.evidence):
            raise ValueError("duplicate claim/evidence IDs")
        if len({r.relation_id for r in self.relations}) != len(self.relations):
            raise ValueError("duplicate relation IDs")
        if len(set(self.pages_processed)) != len(self.pages_processed):
            raise ValueError("duplicate processed pages")
        for item in [*self.claims, *self.evidence]:
            if item.document_id != self.document_id:
                raise ValueError("cross-document extraction is forbidden")
        for item in self.evidence:
            if item.page is None or item.page not in self.pages_processed:
                raise ValueError("PDF evidence must refer to an actually processed page")
        for item in [*self.claims, *self.relations]:
            if not set(item.evidence_ids) <= evidence.keys():
                raise ValueError("unknown evidence reference")
        for item in self.relations:
            if item.from_claim_id not in claims or item.to_claim_id not in claims:
                raise ValueError("relation endpoints must belong to this PDF")
            if item.from_claim_id == item.to_claim_id:
                raise ValueError("self relation")
        for issue in self.issues:
            if not set(issue.affected_claim_ids) <= claims.keys():
                raise ValueError("unknown issue claim reference")
        return self


class Citation(Contract):
    kind: Literal["evidence", "feature", "calculation"]
    ref_id: ID


class FeatureCandidate(Contract):
    feature_id: ID
    name: ID
    text_value: str | None = None
    numeric_value: NumericValue | None = None
    boolean_value: bool | None = None
    # The value can be absent when only a conflict/missingness is known.
    status: Literal["observed", "self_reported", "derived", "conflicting", "missing"]
    period: TimeScope
    source_refs: list[Citation]
    note: str | None = None

    @model_validator(mode="after")
    def usable_value_has_source(self):
        values = [self.text_value, self.numeric_value, self.boolean_value]
        count = sum(v is not None for v in values)
        if count > 1:
            raise ValueError("feature has multiple incompatible value representations")
        if self.status in {"observed", "self_reported", "derived"}:
            if count != 1 or not self.source_refs:
                raise ValueError("usable feature requires a value and provenance")
        if self.status in {"conflicting", "missing"} and count:
            raise ValueError("unresolved feature must not expose a resolved value")
        return self


class CompanyProfileExtraction(Contract):
    document_id: ID
    features: list[FeatureCandidate]
    issues: list[ShortText]


class CompanyProfile(Contract):
    company_id: ID
    version: int = Field(ge=1)
    as_of: AwareDatetime
    features: list[FeatureCandidate]
    unresolved_identity: bool
    source_snapshot_ids: list[ID]


class ScenarioInput(Contract):
    formula: Literal["floating_debt_rate_delta"]
    mode: Literal["company_estimate", "hypothetical"]
    principal_rub: Decimal = Field(gt=0, allow_inf_nan=False)
    delta_rate_pp: Decimal = Field(allow_inf_nan=False)
    horizon_days: int = Field(ge=1, le=366)
    day_count_basis: Literal[360, 365, 366]
    principal_refs: list[Citation]
    rate_refs: list[Citation]
    assumptions: list[ShortText]

    @model_validator(mode="after")
    def assumptions_and_provenance(self):
        if self.mode == "company_estimate" and (not self.principal_refs or not self.rate_refs):
            raise ValueError("company estimate requires principal and rate provenance")
        if self.mode == "hypothetical" and not self.assumptions:
            raise ValueError("hypothetical input must be labeled explicitly")
        return self


class CalculationRecord(Contract):
    calculation_id: ID
    formula_version: ID
    inputs: ScenarioInput
    result_rub: Decimal = Field(allow_inf_nan=False)
    rounding: Literal["decimal_half_even_2dp_for_display"]
    interpretation: ShortText
    created_at: AwareDatetime


class EventBrief(Contract):
    event_id: ID
    document_ids: list[ID] = Field(min_length=1)
    headline: ShortText
    published_at: date | None
    available_at: AwareDatetime


class ProfileTool(Contract):
    tool: Literal["get_company_profile"]


class CompanySearchTool(Contract):
    tool: Literal["search_company_documents"]
    query: Annotated[str, Field(min_length=1, max_length=600)]
    limit: int = Field(default=5, ge=1, le=8)


class EventSearchTool(Contract):
    tool: Literal["search_events"]
    query: Annotated[str, Field(min_length=1, max_length=600)]
    lookback_days: int = Field(default=30, ge=1, le=3660)
    limit: int = Field(default=5, ge=1, le=8)


class BankSearchTool(Contract):
    tool: Literal["search_bank_claims"]
    query: Annotated[str, Field(min_length=1, max_length=600)]
    limit: int = Field(default=5, ge=1, le=8)


class EvidenceTool(Contract):
    tool: Literal["read_evidence"]
    evidence_ids: list[ID] = Field(min_length=1, max_length=8)


class CalculateTool(Contract):
    tool: Literal["calculate_scenario"]
    inputs: ScenarioInput


ToolCall = Annotated[
    ProfileTool | CompanySearchTool | EventSearchTool | BankSearchTool | EvidenceTool | CalculateTool,
    Field(discriminator="tool"),
]


class ResearchStep(Contract):
    level: Literal["world", "cis_ru", "ru", "industry", "company", "competitor", "product_metric"]
    question: ShortText
    why_changes_decision: ShortText


class ToolDecision(Contract):
    kind: Literal["tool"]
    reason_short: ShortText
    action: ToolCall


class AnalyzeDecision(Contract):
    kind: Literal["analyze"]
    reason_short: ShortText


class AskDecision(Contract):
    kind: Literal["ask_user"]
    field: Literal[
        "floating_debt_rub", "rate_type", "fx_exposure", "decision_horizon", "identity_mapping", "other"
    ]
    question: Annotated[str, Field(min_length=1, max_length=500)]
    why_needed: ShortText


class NoRelevantDecision(Contract):
    kind: Literal["no_relevant_event"]
    reason_short: ShortText
    checked_feature_ids: list[ID] = Field(min_length=1)


class ControllerOutput(Contract):
    open_questions: list[ShortText] = Field(max_length=8)
    proposed_plan: list[ResearchStep] = Field(max_length=7)
    next: Annotated[
        ToolDecision | AnalyzeDecision | AskDecision | NoRelevantDecision,
        Field(discriminator="kind"),
    ]


class InsightStatement(Contract):
    statement_id: ID
    text: ShortText
    claim_type: ClaimType
    owner: StatementOwner
    derivation: Literal["quoted_source", "ai_inference", "calculated", "conditional"]
    time_scope: TimeScope
    conditions: list[ShortText]
    citations: list[Citation] = Field(min_length=1)


class Impact(Contract):
    direction: Literal["risk", "opportunity", "mixed", "unknown"]
    materiality: Literal["high", "medium", "low", "unknown"]
    evidence_strength: Literal["supported", "conditional", "insufficient"]
    urgency: Literal["now", "planned", "monitor", "unknown"]
    mechanism_statement_ids: list[ID] = Field(min_length=1)
    exposed_feature_ids: list[ID] = Field(min_length=1)
    horizon: TimeScope


class SuggestedAction(Contract):
    title: ShortText
    rationale_statement_ids: list[ID] = Field(min_length=1)
    target_roles: list[
        Literal["treasurer", "cfo", "ceo", "coo", "cco", "owner", "chief_accountant", "rm"]
    ] = Field(min_length=1)
    required_data: list[ShortText]


class InsightCore(Contract):
    event_ids: list[ID] = Field(min_length=1)
    headline: Annotated[str, Field(min_length=1, max_length=180)]
    statements: list[InsightStatement] = Field(min_length=1)
    impact: Impact
    actions: list[SuggestedAction]
    bank_claim_ids_used: list[ID]
    bank_coverage: Literal["applicable", "historical_only", "not_found", "unresolved"]
    limitations: list[ShortText]
    missing_information: list[ShortText]

    @model_validator(mode="after")
    def internal_refs(self):
        ids = {s.statement_id for s in self.statements}
        if len(ids) != len(self.statements):
            raise ValueError("duplicate statement IDs")
        for refs in [self.impact.mechanism_statement_ids, *[a.rationale_statement_ids for a in self.actions]]:
            if not set(refs) <= ids:
                raise ValueError("unknown insight statement reference")
        if self.bank_coverage in {"applicable", "historical_only"} and not self.bank_claim_ids_used:
            raise ValueError("bank coverage requires actual source claims")
        if self.bank_coverage in {"not_found", "unresolved"} and not self.limitations:
            raise ValueError("missing bank coverage must be visible")
        return self


class VerificationIssue(Contract):
    severity: Literal["blocking", "warning"]
    code: Literal[
        "unsupported", "wrong_owner", "wrong_time", "contradiction", "wrong_number", "missing_data", "wording"
    ]
    statement_ids: list[ID]
    explanation: ShortText
    proposed_fix: ShortText


class VerifierOutput(Contract):
    verdict: Literal["pass", "needs_evidence", "revise", "clarify"]
    issues: list[VerificationIssue]

    @model_validator(mode="after")
    def no_blockers_on_pass(self):
        if self.verdict == "pass" and any(i.severity == "blocking" for i in self.issues):
            raise ValueError("blocking issue cannot pass")
        if self.verdict != "pass" and not self.issues:
            raise ValueError("non-passing verdict requires a reason")
        return self


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    DEFERRED_BUDGET = "DEFERRED_BUDGET"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Node(StrEnum):
    HYDRATE = "HYDRATE"
    PLAN = "PLAN"
    TOOL_EXEC = "TOOL_EXEC"
    ANALYZE = "ANALYZE"
    VERIFY = "VERIFY"
    PROJECT = "PROJECT"
    PUBLISH = "PUBLISH"
    FINISHED = "FINISHED"


class TrustedJobContext(Contract):
    """Created from auth/API, never parsed from model messages."""

    run_id: ID
    input_revision: int = Field(ge=1)
    tenant_id: ID
    user_id: ID
    company_id: ID
    permitted_document_ids: list[ID]
    allowed_actions: list[Literal["read", "ask", "calculate"]]
    role_preference: Literal["treasurer", "cfo", "ceo", "coo", "cco", "owner", "chief_accountant", "rm"]
    as_of: AwareDatetime
    idempotency_key: ID


class RunState(Contract):
    """Server-owned checkpoint; models only receive a filtered ModelView."""

    run_id: ID
    input_revision: int = Field(ge=1)
    state_version: int = Field(ge=1)
    status: RunStatus
    node: Node
    profile_version: int | None = Field(default=None, ge=1)
    event_ids: list[ID]
    evidence_ids: list[ID]
    calculation_ids: list[ID]
    plan: list[ResearchStep]
    open_questions: list[ShortText]
    llm_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    active_elapsed_ms: int = Field(default=0, ge=0)
    pending_question_id: ID | None = None
    next_eligible_at: AwareDatetime | None = None
    last_error_code: str | None = None
    draft_id: ID | None = None
    verified_core_id: ID | None = None
    publish_outcome: Literal["complete", "partial", "no_relevant_event"] | None = None

    @model_validator(mode="after")
    def suspension_is_explicit(self):
        if self.status == RunStatus.WAITING_INPUT and not self.pending_question_id:
            raise ValueError("waiting state requires a persisted question")
        if self.status == RunStatus.DEFERRED_BUDGET and self.next_eligible_at is None:
            raise ValueError("budget suspension requires reset time")
        if self.status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL} and self.node != Node.FINISHED:
            raise ValueError("published run must be finished")
        if self.status == RunStatus.PARTIAL and self.publish_outcome != "partial":
            raise ValueError("partial run requires explicit partial outcome")
        if self.status == RunStatus.SUCCEEDED and self.publish_outcome not in {
            "complete",
            "no_relevant_event",
        }:
            raise ValueError("successful run requires explicit outcome")
        if self.status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL}:
            if self.publish_outcome != "no_relevant_event" and not self.verified_core_id:
                raise ValueError("published insight requires a verified core")
        return self


class ToolObservation(Contract):
    observation_id: ID
    tool: Literal[
        "get_company_profile",
        "search_company_documents",
        "search_events",
        "search_bank_claims",
        "read_evidence",
        "calculate_scenario",
    ]
    outcome: Literal["ok", "empty", "blocked", "recoverable_error"]
    summary: ShortText
    evidence_ids: list[ID]
    feature_ids: list[ID]
    calculation_ids: list[ID]
    error_code: str | None = None


class ModelView(Contract):
    """Built by the server after checking ACL, snapshot, size and provenance.

    Full raw files/ACL/secrets are absent. An ID in this view does not grant
    authorization: the tool runner checks access again when resolving it.
    """

    task: ShortText
    as_of: AwareDatetime
    role_preference: str
    profile: CompanyProfile | None
    triggering_event_ids: list[ID]
    events: list[EventBrief]
    documents: list[DocumentRef]
    evidence: list[Evidence] = Field(max_length=128)
    bank_claims: list[SourceClaim]
    calculations: list[CalculationRecord]
    observations: list[ToolObservation]
    open_questions: list[ShortText]
    previous_verification: VerifierOutput | None = None
    remaining_llm_calls: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)
    remaining_replans: int = Field(ge=0)
    remaining_active_ms: int = Field(ge=0)


MODEL_OUTPUTS = {
    "controller": ControllerOutput,
    "bank_extract": BankExtraction,
    "company_profile": CompanyProfileExtraction,
    "analyst": InsightCore,
    "verifier": VerifierOutput,
}

# Useful for schema generation / validation, not a replacement for authorization.
TOOL_ADAPTER = TypeAdapter(ToolCall)

from pydantic import Field
from .model_outputs import AnalysisDraft

from .domain import (
    AskDecision,
    CompanyProfile,
    ControllerOutput,
    DocumentRef,
    Evidence,
    InsightCore,
    RunState,
    VerifierOutput,
)


class RuntimeState(RunState):
    replan_tool_baseline: int = 0
    analysis: AnalysisDraft | None = None
    bank_claim_ids: list[str] = Field(default_factory=list)
    profile: CompanyProfile | None = None
    controller: ControllerOutput | None = None
    draft: InsightCore | None = None
    verification: VerifierOutput | None = None
    question: AskDecision | None = None
    answer_documents: list[DocumentRef] = Field(default_factory=list)
    answer_evidence: list[Evidence] = Field(default_factory=list)
    skipped_fields: list[str] = Field(default_factory=list)
    observed_tools: list[str] = Field(default_factory=list)
    seen_tool_calls: list[str] = Field(default_factory=list)
    calculation_records: list[dict] = Field(default_factory=list)
    observations: list[dict] = Field(default_factory=list)
    gate_issues: list[dict] = Field(default_factory=list)
    outcome_mode: str = "live"

"""Small model-facing contracts; source facts remain server-owned."""

from typing import Annotated, Literal
from pydantic import Field, model_validator, ValidationInfo
from .domain import AnalyzeDecision, AskDecision, Contract, NoRelevantDecision, ShortText, ToolDecision


class StepPlan(Contract):
    question: ShortText
    next: Annotated[
        ToolDecision | AnalyzeDecision | AskDecision | NoRelevantDecision, Field(discriminator="kind")
    ]


class RoleAnalysis(Contract):
    role: Literal["cfo", "treasurer", "ceo", "coo", "cco"]
    mechanism: Literal[
        "floating_budget",
        "floating_amount_unknown",
        "fixed_budget",
        "floating_contracts",
        "fixed_contracts",
        "capex_funding",
        "inventory_funding",
        "receivables_funding",
        "debt_structure_unknown",
        "treasury_unknown",
        "deposit_budget",
        "deposit_contracts",
        "strategy_context",
        "operations_context",
        "commercial_context",
    ]
    action_id: Literal[
        "floating_budget",
        "floating_amount_unknown",
        "fixed_budget",
        "floating_contracts",
        "fixed_contracts",
        "capex_funding",
        "inventory_funding",
        "receivables_funding",
        "debt_structure_unknown",
        "treasury_unknown",
        "deposit_budget",
        "deposit_contracts",
        "strategy_context",
        "operations_context",
        "commercial_context",
    ]
    bank_claim_ids: list[str] = Field(max_length=3)

    @model_validator(mode="after")
    def matches_current_catalogue(self, info: ValidationInfo):
        catalogue = (info.context or {}).get("role_catalogue")
        if catalogue:
            expected = catalogue[self.role]
            if self.mechanism != expected["mechanism"] or self.action_id != expected["action_id"]:
                raise ValueError("Choose this role's mechanism/action_id exactly from role_catalogue")
        return self


def controller_schema(allowed_tools, allowed_decisions):
    schema = StepPlan.model_json_schema()
    defs = schema["$defs"]
    choice = schema["properties"]["next"]
    mapping = choice["discriminator"]["mapping"]
    choice["oneOf"] = [{"$ref": ref} for kind, ref in mapping.items() if kind in allowed_decisions]
    choice["discriminator"]["mapping"] = {
        kind: ref for kind, ref in mapping.items() if kind in allowed_decisions
    }
    if "tool" in allowed_decisions:
        tool = defs["ToolDecision"]["properties"]["action"]
        toolmap = tool["discriminator"]["mapping"]
        tool["oneOf"] = [{"$ref": ref} for kind, ref in toolmap.items() if kind in allowed_tools]
        tool["discriminator"]["mapping"] = {
            kind: ref for kind, ref in toolmap.items() if kind in allowed_tools
        }
    seen = set()

    def collect(value):
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].rsplit("/", 1)[-1]
                if name not in seen:
                    seen.add(name)
                    collect(defs[name])
            for key, item in value.items():
                if key != "$defs":
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(schema)
    schema["$defs"] = {name: defs[name] for name in seen}
    return schema


class AnalysisDraft(Contract):
    roles: list[RoleAnalysis] = Field(min_length=5, max_length=5)
    direction: Literal["risk", "opportunity", "mixed", "unknown"]
    materiality: Literal["high", "medium", "low", "unknown"]
    urgency: Literal["now", "planned", "monitor", "unknown"]
    limitations: list[ShortText] = Field(max_length=5)

    @model_validator(mode="after")
    def five_distinct_roles(self):
        if {r.role for r in self.roles} != {"cfo", "treasurer", "ceo", "coo", "cco"}:
            raise ValueError("Provide cfo, treasurer, ceo, coo and cco exactly once")
        return self

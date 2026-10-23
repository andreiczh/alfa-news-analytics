from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from alfa_agent.service import Service
from alfa_agent.settings import Settings


@pytest.fixture
def service(tmp_path):
    (tmp_path / "data").mkdir()
    shutil.copy2(Path(__file__).parents[1] / "data/demo_bundle.json", tmp_path / "data/demo_bundle.json")
    (tmp_path / ".env.local").write_text("ALFA_LLM_API_KEY=test-only-value\n")
    value = Service(Settings(root=tmp_path))
    yield value
    value.corpus.close()


class ScenarioGateway:
    """Test double, never used by the product or presented as live LLM output."""

    mode = "test_double"

    def __init__(self, bad_json=False, revise_once=False, fail=None, on_call=None):
        self.bad_json = bad_json
        self.revise_once = revise_once
        self.fail = fail
        self.on_call = on_call
        self.calls = []

    async def complete(self, run_id, stage, payload, schema, repair=None):
        self.calls.append(stage)
        if self.on_call:
            self.on_call()
        if self.fail:
            raise self.fail
        if self.bad_json:
            self.bad_json = False
            return "not JSON"
        context = payload["context"]
        if stage == "controller":
            observed = {o["tool"] for o in context["observations"]}
            if not context["profile"]:
                decision = {
                    "kind": "tool",
                    "reason_short": "Read real profile from tool",
                    "action": {"tool": "get_company_profile"},
                }
            elif "search_bank_claims" not in observed:
                decision = {
                    "kind": "tool",
                    "reason_short": "Find dated bank sources",
                    "action": {"tool": "search_bank_claims", "query": "ключевая ставка прогноз пауза"},
                }
            else:
                debt = next(f for f in context["profile"]["features"] if f["name"] == "floating_debt_rub")
                if (
                    debt["status"] == "missing"
                    and "floating_debt_rub" not in payload["policy"]["skipped_fields"]
                ):
                    if "search_company_documents" not in observed:
                        decision = {
                            "kind": "tool",
                            "reason_short": "Check files before asking",
                            "action": {"tool": "search_company_documents", "query": "плавающий долг остаток"},
                        }
                    else:
                        decision = {
                            "kind": "ask_user",
                            "field": "floating_debt_rub",
                            "question": "Каков остаток плавающего долга в рублях?",
                            "why_needed": "Нужен для чувствительности расходов",
                        }
                elif payload["scenario_request"] and not context["calculations"]:
                    decision = {
                        "kind": "tool",
                        "reason_short": "Calculate with verified inputs",
                        "action": {"tool": "calculate_scenario", "inputs": payload["scenario_request"]},
                    }
                else:
                    decision = {"kind": "analyze", "reason_short": "Available evidence is sufficient"}
            return json.dumps({"question": "Закрыть текущий существенный пробел", "next": decision})
        if stage == "verifier":
            if self.revise_once:
                self.revise_once = False
                return json.dumps(
                    {
                        "verdict": "revise",
                        "issues": [
                            {
                                "severity": "blocking",
                                "code": "wording",
                                "statement_ids": ["ai_cfo"],
                                "explanation": "Clarify conditional wording",
                                "proposed_fix": "Keep assumptions explicit",
                            }
                        ],
                    }
                )
            return '{"verdict":"pass","issues":[]}'
        roles = []
        for role, selection in payload["role_catalogue"].items():
            roles.append(
                {
                    "role": role,
                    "mechanism": selection["mechanism"],
                    "action_id": selection["action_id"],
                    "bank_claim_ids": [
                        next(
                            (c["claim_id"] for c in context["bank_claims"] if c["claim_type"] == "FORECAST"),
                            context["bank_claims"][0]["claim_id"],
                        )
                    ],
                }
            )
        return json.dumps(
            {
                "roles": roles,
                "direction": "mixed",
                "materiality": "unknown",
                "urgency": "planned",
                "limitations": ["Синтетический профиль и условный сценарий."],
            },
            ensure_ascii=False,
        )

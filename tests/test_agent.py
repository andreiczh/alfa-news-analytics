import asyncio
import json
from decimal import Decimal

import pytest

from alfa_agent.corpus import AccessDenied
from alfa_agent.domain import TOOL_ADAPTER
from alfa_agent.engine import Engine
from alfa_agent.gateway import GatewayError
from alfa_agent.service import Answer, CreateRun
from alfa_agent.state import RuntimeState
from alfa_agent.storage import Conflict
from alfa_agent.tools import expected_scenario
from alfa_agent.verification import check_core
from conftest import ScenarioGateway


def execute(service, gateway=None):
    engine = Engine(service.settings, service.store, service.corpus, gateway or ScenarioGateway())
    asyncio.run(engine.once())
    return engine


def test_real_tool_loop_and_five_role_views(service):
    run = service.create(CreateRun(), "e2e")
    execute(service)
    result = service.get(run["run_id"])
    assert result["status"] == "SUCCEEDED"
    assert {r["role"] for r in result["result"]["role_views"]} == {"cfo", "treasurer", "ceo", "coo", "cco"}
    assert result["result"]["calculations"][0]["result_rub"].startswith("-2465753.424")
    assert len([s for s in result["trace"] if s["event"] == "tool_completed"]) == 3
    assert result["llm_calls"] <= service.settings.max_calls
    actions = {r["role"]: r["actions"][0]["title"] for r in result["result"]["role_views"]}
    assert len(set(actions.values())) == 5


def test_missing_debt_asks_then_resumes_with_provenance(service):
    run = service.create(CreateRun(company_id="demo_missing"), "missing")
    execute(service)
    wait = service.get(run["run_id"])
    assert wait["status"] == "WAITING_INPUT" and not wait["result"]
    answer = Answer(
        answer_id="answer_once",
        question_id=wait["question_id"],
        expected_version=wait["version"],
        value_rub="1000000000",
    )
    after = service.answer(run["run_id"], answer)
    # Duplicate answer is idempotent even though its expected version is old.
    assert service.answer(run["run_id"], answer)["version"] == after["version"]
    execute(service)
    final = service.get(run["run_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["result"]["profile_version"] == 2
    assert final["llm_calls"] > wait["llm_calls"]
    state = RuntimeState.model_validate(service.store.get(run["run_id"])["state"])
    assert state.answer_documents[0].tenant_company_id == "demo_missing"
    assert state.input_revision == 2


def test_skipped_answer_does_not_fabricate_calculation(service):
    run = service.create(CreateRun(company_id="demo_missing"), "skip")
    execute(service)
    wait = service.get(run["run_id"])
    service.answer(
        run["run_id"],
        Answer(
            answer_id="skip_once",
            question_id=wait["question_id"],
            expected_version=wait["version"],
            skip=True,
        ),
    )
    execute(service)
    final = service.get(run["run_id"])
    assert final["status"] == "PARTIAL"
    assert final["result"]["calculations"] == []


def test_fixed_rate_has_no_floating_sensitivity(service):
    run = service.create(CreateRun(company_id="demo_fixed"), "fixed")
    execute(service)
    final = service.get(run["run_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["result"]["calculations"] == []


def test_json_repair_counted_and_bounded(service):
    run = service.create(CreateRun(), "repair")
    execute(service, ScenarioGateway(bad_json=True))
    final = service.get(run["run_id"])
    assert final["status"] == "SUCCEEDED"
    assert sum(s["event"] == "json_repair" for s in final["trace"]) == 1


def test_verifier_really_causes_revision(service):
    run = service.create(CreateRun(), "replan")
    execute(service, ScenarioGateway(revise_once=True))
    final = service.get(run["run_id"])
    assert final["status"] == "SUCCEEDED" and final["replans"] == 1
    assert any(s["event"] == "replanned" for s in final["trace"])


def test_cancel_during_model_call_cannot_publish(service):
    run = service.create(CreateRun(), "cancel")
    gateway = ScenarioGateway(on_call=lambda: service.store.cancel(run["run_id"]))
    execute(service, gateway)
    final = service.get(run["run_id"])
    assert final["status"] == "CANCELLED" and final["result"] is None


def test_budget_exhaustion_checkpoint_not_success(service):
    run = service.create(CreateRun(), "budget")
    execute(
        service, ScenarioGateway(fail=GatewayError("shared_budget_exhausted", "2099-01-01T03:00:00+03:00"))
    )
    final = service.get(run["run_id"])
    assert final["status"] == "DEFERRED_BUDGET" and final["result"] is None


def test_auth_error_is_finite(service):
    run = service.create(CreateRun(), "auth")
    execute(service, ScenarioGateway(fail=GatewayError("api_auth_failed")))
    final = service.get(run["run_id"])
    assert final["status"] == "FAILED" and final["error_code"] == "api_auth_failed"


def test_unknown_company_and_foreign_evidence_blocked(service):
    with pytest.raises(AccessDenied):
        service.create(CreateRun(company_id="forged_company"), "foreign")
    with pytest.raises(AccessDenied):
        service.corpus.read_evidence(
            ["demo_fixed_evidence"], "demo_floating", service.corpus.as_of("2026-10-23T10:00:00Z")
        )


def test_run_idempotency_and_conflict(service):
    first = service.create(CreateRun(), "idem")
    assert service.create(CreateRun(), "idem")["run_id"] == first["run_id"]
    with pytest.raises(Conflict):
        service.create(CreateRun(company_id="demo_fixed"), "idem")


def test_wrong_answer_version_rejected(service):
    run = service.create(CreateRun(company_id="demo_missing"), "stale")
    execute(service)
    wait = service.get(run["run_id"])
    with pytest.raises(Conflict):
        service.answer(
            run["run_id"],
            Answer(answer_id="stale", question_id=wait["question_id"], expected_version=1, value_rub="10"),
        )


def test_no_arbitrary_tool_or_scope_argument():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TOOL_ADAPTER.validate_python({"tool": "shell", "command": "irrelevant"})
    with pytest.raises(ValidationError):
        TOOL_ADAPTER.validate_python({"tool": "get_company_profile", "company_id": "forged"})


def test_forged_calculation_input_blocked(service):
    engine = Engine(service.settings, service.store, service.corpus, ScenarioGateway())
    run = service.create(CreateRun(), "forge")
    state = RuntimeState.model_validate(service.store.get(run["run_id"])["state"])
    state.profile = service.corpus.profile("demo_floating")
    request = service.store.get(run["run_id"])["input"]
    inputs = expected_scenario(state, request)
    inputs["principal_rub"] = "2000000000"
    with pytest.raises(ValueError, match="scenario_inputs"):
        engine.tools.run(
            TOOL_ADAPTER.validate_python({"tool": "calculate_scenario", "inputs": inputs}), state, request
        )


def test_nonexistent_citation_cannot_pass_deterministic_gate(service):
    run = service.create(CreateRun(), "refs")
    engine = execute(service)
    row = service.store.get(run["run_id"])
    state = RuntimeState.model_validate(row["state"])
    core = state.draft.model_copy(deep=True)
    core.statements[0].citations[0].ref_id = "invented_evidence"
    assert any(i.code == "unsupported" for i in check_core(core, state, engine.view(state, row["input"])))


def test_bank_forecast_cannot_be_promoted_to_fact(service):
    run = service.create(CreateRun(), "forecast")
    engine = execute(service)
    row = service.store.get(run["run_id"])
    state = RuntimeState.model_validate(row["state"])
    core = state.draft.model_copy(deep=True)
    next(s for s in core.statements if s.claim_type == "FORECAST").claim_type = "FACT"
    assert any(i.code == "wrong_time" for i in check_core(core, state, engine.view(state, row["input"])))


def test_wrong_numeric_effect_blocked(service):
    run = service.create(CreateRun(), "number")
    engine = execute(service)
    row = service.store.get(run["run_id"])
    state = RuntimeState.model_validate(row["state"])
    core = state.draft.model_copy(deep=True)
    next(
        s for s in core.statements if s.derivation == "calculated"
    ).text = "Экономия составит 999999999 рублей."
    assert any(i.code == "wrong_number" for i in check_core(core, state, engine.view(state, row["input"])))


def test_decimal_amount_exact_to_display(service):
    run = service.create(CreateRun(), "decimal")
    execute(service)
    view = service.get(run["run_id"])["result"]["role_views"][0]
    assert Decimal(view["scenario"]["amount_rub"]) == Decimal("-2465753.42")


def test_model_materiality_is_not_published_without_denominator(service):
    class Overconfident(ScenarioGateway):
        async def complete(self, *args, **kwargs):
            raw = await super().complete(*args, **kwargs)
            if args[1] == "analyst":
                data = json.loads(raw)
                data["materiality"] = "high"
                raw = json.dumps(data)
            return raw

    run = service.create(CreateRun(), "unmeasured-materiality")
    execute(service, Overconfident())
    assert service.get(run["run_id"])["result"]["core"]["impact"]["materiality"] == "unknown"

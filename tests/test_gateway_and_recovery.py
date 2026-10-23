import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from alfa_agent.domain import ControllerOutput
from alfa_agent.gateway import GatewayError, LiveGateway
from alfa_agent.service import CreateRun
from alfa_agent.settings import next_reset
from alfa_agent.state import RuntimeState
from alfa_agent.verification import numbers


def test_budget_reset_before_and_at_three_msk():
    before = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc)  # 02:59:59 next day MSK
    at = before + timedelta(seconds=1)
    assert next_reset(before).isoformat() == "2026-10-24T03:00:00+03:00"
    assert next_reset(at).isoformat() == "2026-10-25T03:00:00+03:00"


def test_dates_are_not_decimal_amounts():
    assert [str(x) for x, _ in numbers("11.09.2026")] == ["11", "9", "2026"]


def test_source_unsupported_warning_cannot_pass():
    from alfa_agent.domain import VerifierOutput
    from alfa_agent.verification import enforce_verification_policy

    output = VerifierOutput(
        verdict="pass",
        issues=[
            {
                "severity": "warning",
                "code": "unsupported",
                "statement_ids": ["ai_cco"],
                "explanation": "An instrument was invented",
                "proposed_fix": "Remove the unsupported fact",
            }
        ],
    )
    result = enforce_verification_policy(output)
    assert result.verdict == "revise" and result.issues[0].severity == "blocking"


def test_controller_only_sees_tools_allowed_for_current_gap():
    from alfa_agent.model_outputs import controller_schema

    schema = controller_schema(["search_bank_claims"], ["tool"])
    assert set(schema["$defs"]["ToolDecision"]["properties"]["action"]["discriminator"]["mapping"]) == {
        "search_bank_claims"
    }
    assert "CalculateTool" not in schema["$defs"]


def test_free_financial_prose_is_not_accepted_in_rule_selection():
    from alfa_agent.model_outputs import RoleAnalysis
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoleAnalysis(
            role="cfo",
            mechanism="floating_budget",
            action_id="floating_budget",
            bank_claim_ids=[],
            explanation="Расходы гарантированно снизятся",
        )


@pytest.mark.parametrize(
    "http_status,body,code",
    [
        (401, {"error": "denied"}, "api_auth_failed"),
        (429, {"error": "rate limit"}, "rate_limited"),
        (500, {"error": "unavailable"}, "provider_unavailable"),
        (402, {"error": "Budget has been exceeded"}, "shared_budget_exhausted"),
    ],
)
def test_gateway_errors_are_sanitized_and_finite(service, http_status, body, code):
    async def perform():
        gateway = LiveGateway(
            service.settings,
            service.store,
            transport=httpx.MockTransport(lambda request: httpx.Response(http_status, json=body)),
        )
        try:
            with pytest.raises(GatewayError) as raised:
                await gateway.complete("test_run", "controller", {}, ControllerOutput.model_json_schema())
            assert raised.value.code == code
            assert "test-only-value" not in str(raised.value)
        finally:
            await gateway.close()

    asyncio.run(perform())


def test_gateway_prompt_excludes_secret_and_usage_is_recorded(service):
    def transport(request):
        assert request.headers["Authorization"] == "Bearer test-only-value"
        assert b"test-only-value" not in request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        )

    async def perform():
        gateway = LiveGateway(service.settings, service.store, transport=httpx.MockTransport(transport))
        try:
            assert (
                await gateway.complete("test_run", "controller", {}, ControllerOutput.model_json_schema())
                == "{}"
            )
        finally:
            await gateway.close()

    asyncio.run(perform())
    assert json.loads(service.store.attempts("test_run")[0]["usage_json"])["total_tokens"] == 15


def test_crash_after_external_start_is_not_replayed(service):
    run = service.create(CreateRun(), "crash")
    row = service.store.claim()
    service.store.attempt_start(row["id"], "controller", "hash", service.settings.model)
    with service.store.connection() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (run["run_id"],))
    assert service.store.claim() is None
    state = RuntimeState.model_validate(service.store.get(run["run_id"])["state"])
    assert state.status == "FAILED" and state.last_error_code == "external_result_unknown"


def test_second_worker_cannot_claim_another_job_while_key_in_use(service):
    service.create(CreateRun(), "one")
    service.create(CreateRun(company_id="demo_fixed"), "two")
    assert service.store.claim() is not None
    assert service.store.claim() is None

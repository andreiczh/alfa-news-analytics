from __future__ import annotations

import asyncio
import hashlib
import time

from pydantic import ValidationError

from .assembly import assemble_core
from .corpus import AccessDenied
from .domain import (
    CalculationRecord,
    ControllerOutput,
    ResearchStep,
    ModelView,
    Node,
    RunStatus,
    TOOL_ADAPTER,
    ToolObservation,
    VerifierOutput,
)
from .gateway import GatewayError
from .model_outputs import AnalysisDraft, StepPlan, controller_schema
from .roles import project_roles
from .role_rules import catalogue
from .settings import Settings
from .state import RuntimeState
from .storage import Conflict, new_id
from .tools import ToolRunner, expected_scenario, feature
from .verification import check_core


def initial_state(run_id, mode="live"):
    return RuntimeState(
        run_id=run_id,
        input_revision=1,
        state_version=1,
        status="QUEUED",
        node="HYDRATE",
        event_ids=["rate_decision_20260911"],
        evidence_ids=["news_rate_fact"],
        calculation_ids=[],
        plan=[],
        open_questions=[],
        outcome_mode=mode,
    )


class Engine:
    def __init__(self, settings: Settings, store, corpus, gateway):
        self.settings, self.store, self.corpus, self.gateway = settings, store, corpus, gateway
        self.tools = ToolRunner(corpus)

    def view(self, state, request):
        cutoff = self.corpus.as_of(request["as_of"])
        company = request["company_id"]
        answer_ev = {e.evidence_id: e for e in state.answer_evidence}
        answer_docs = {d.document_id: d for d in state.answer_documents}
        found = []
        for eid in state.evidence_ids:
            if eid in answer_ev:
                item = answer_ev[eid]
                doc = answer_docs[item.document_id]
                if doc.tenant_company_id != company or doc.available_at > cutoff:
                    raise AccessDenied("answer_outside_scope")
            else:
                item = self.corpus.read_evidence([eid], company, cutoff)[0]
            found.append(item)
        documents = []
        for did in dict.fromkeys(e.document_id for e in found):
            documents.append(
                answer_docs[did] if did in answer_docs else self.corpus.authorize(did, company, cutoff)
            )
        if state.profile and (state.profile.company_id != company or state.profile.as_of > cutoff):
            raise AccessDenied("profile_outside_scope")
        if len(found) > 128:
            raise GatewayError("context_limit")
        return ModelView(
            task="Разобрать событие о сохранении ставки 11 сентября 2026 для данной компании. "
            "Разделить факт новости, прогноз банка на его дату и условную чувствительность. "
            "Это разбор датированного события, не заявление о самой свежей позиции банка. "
            "Обосновать действия для CFO, казначея, CEO, COO и CCO при наличии признаков. "
            "Не считать сохранение ставки немедленной экономией. Не придумывать новые цифры.",
            as_of=cutoff,
            role_preference="cfo",
            profile=state.profile,
            triggering_event_ids=state.event_ids,
            events=[self.corpus.events[x] for x in state.event_ids],
            documents=documents,
            evidence=found,
            bank_claims=[self.corpus.claims[x] for x in state.bank_claim_ids],
            calculations=[CalculationRecord.model_validate(x) for x in state.calculation_records],
            observations=[ToolObservation.model_validate(x) for x in state.observations[-8:]],
            open_questions=state.open_questions,
            previous_verification=state.verification,
            remaining_llm_calls=max(0, self.settings.max_calls - state.llm_calls),
            remaining_tool_calls=max(0, self.settings.max_tools - state.tool_calls),
            remaining_replans=max(0, self.settings.max_replans - state.replans),
            remaining_active_ms=max(0, self.settings.active_seconds * 1000 - state.active_elapsed_ms),
        )

    async def once(self, target_run_id=None):
        row = self.store.claim(self.settings.lease_seconds, target_run_id)
        if not row:
            return None
        await self.execute(row)
        return row["id"]

    async def forever(self):
        while True:
            if not await self.once():
                await asyncio.sleep(0.5)

    async def execute(self, row):
        self.corpus.load_run_snapshot(row["input"])
        state = RuntimeState.model_validate(row["state"])
        state.outcome_mode = self.gateway.mode
        request = row["input"]
        last_tick = time.monotonic()

        def save(result=None, final_audit=None):
            nonlocal row, last_tick
            now = time.monotonic()
            state.active_elapsed_ms += int((now - last_tick) * 1000)
            last_tick = now
            state.state_version = row["version"] + 1
            row = self.store.save(row, state.model_dump(mode="json"), result, final_audit)
            self.store.renew(row["id"], self.settings.lease_seconds)

        def audit(event, data):
            self.store.audit(row["id"], state.node.value, event, data)

        def limit():
            current = self.store.get(row["id"])
            if current["status"] == "CANCELLED" or current["version"] != row["version"]:
                raise Conflict("run_changed")
            if (
                state.active_elapsed_ms + int((time.monotonic() - last_tick) * 1000)
                > self.settings.active_seconds * 1000
            ):
                raise GatewayError("active_time_limit")

        async def invoke(stage, cls, payload, output_schema=None):
            repair = None
            for attempt in range(2):
                limit()
                if state.llm_calls >= self.settings.max_calls:
                    raise GatewayError("llm_call_limit")
                state.llm_calls += 1
                save()
                audit(
                    "llm_started",
                    {
                        "stage": stage,
                        "call": state.llm_calls,
                        "repair": bool(attempt),
                        "model": self.settings.model,
                    },
                )
                raw = await self.gateway.complete(
                    row["id"], stage, payload, output_schema or cls.model_json_schema(), repair=repair
                )
                try:
                    result = cls.model_validate_json(raw, context=payload)
                    audit("llm_validated", {"stage": stage})
                    return result
                except ValidationError as error:
                    errors = error.errors(include_input=False, include_context=False, include_url=False)
                    audit(
                        "json_repair" if attempt == 0 else "json_rejected",
                        {
                            "stage": stage,
                            "errors": [
                                {"type": e["type"], "location": list(e["loc"]), "message": e["msg"]}
                                for e in errors
                            ],
                        },
                    )
                    repair = {"raw": raw, "errors": errors}
            raise GatewayError("model_contract_invalid")

        def policy_note(message):
            state.open_questions = [message]
            audit("policy_correction", {"reason": message})

        def needs_debt_amount():
            rate = feature(state, "rate_type")
            return bool(
                rate
                and rate.text_value == "floating"
                and not feature(state, "floating_debt_rub")
                and "floating_debt_rub" not in state.skipped_fields
            )

        def wait_question(question):
            state.question = question.model_copy(
                update={
                    "question": "Каков текущий остаток долга с плавающей ставкой, в рублях? Можно пропустить и получить качественный сценарий.",
                    "why_needed": "Сумма нужна для условной чувствительности процентных расходов; отсутствие данных не заменяется оценкой.",
                }
            )
            state.pending_question_id = new_id("question")
            state.status = RunStatus.WAITING_INPUT
            audit(
                "question_created",
                {
                    "question_id": state.pending_question_id,
                    "field": question.field,
                    "why": question.why_needed,
                },
            )
            save()

        try:
            while state.status == RunStatus.RUNNING:
                limit()
                if state.node == Node.HYDRATE:
                    if request["company_id"] not in self.corpus.profiles:
                        raise AccessDenied("company_not_accessible")
                    if state.profile is None:
                        if state.tool_calls >= self.settings.max_tools:
                            raise GatewayError("tool_call_limit")
                        observation = self.tools.run(
                            TOOL_ADAPTER.validate_python({"tool": "get_company_profile"}), state, request
                        )
                        state.tool_calls += 1
                        audit("tool_completed", observation.model_dump(mode="json"))
                    state.node = Node.PLAN
                    audit(
                        "snapshot_ready",
                        {
                            "company_id": request["company_id"],
                            "revision": state.input_revision,
                            "mode": state.outcome_mode,
                        },
                    )
                    save()
                elif state.node == Node.PLAN:
                    # Mandatory context is deterministic. Once the requested evidence
                    # and sensitivity are present, reserve calls for analysis/checking.
                    ready = (
                        state.profile is not None
                        and "search_bank_claims" in state.observed_tools
                        and not needs_debt_amount()
                        and (expected_scenario(state, request) is None or bool(state.calculation_records))
                    )
                    can_stop_search = (
                        state.verification is None
                        or state.verification.verdict != "needs_evidence"
                        or state.tool_calls > state.replan_tool_baseline
                    )
                    if ready and can_stop_search:
                        state.node = Node.ANALYZE
                        audit(
                            "evidence_ready",
                            {
                                "reason": "Required context and requested scenario are available; stop redundant search."
                            },
                        )
                        save()
                        continue
                    view = self.view(state, request)
                    if "search_bank_claims" not in state.observed_tools:
                        allowed_tools = ["search_bank_claims"]
                    elif needs_debt_amount():
                        allowed_tools = (
                            ["search_company_documents"]
                            if "search_company_documents" not in state.observed_tools
                            else []
                        )
                    elif expected_scenario(state, request) and not state.calculation_records:
                        allowed_tools = ["calculate_scenario"]
                    else:
                        allowed_tools = ["search_bank_claims", "read_evidence", "search_company_documents"]
                    payload = {
                        "context": view.model_dump(mode="json"),
                        "allowed_tools": allowed_tools,
                        "allowed_decisions": ["tool"] if allowed_tools else ["ask_user"],
                        "scenario_request": expected_scenario(state, request),
                        "policy": {
                            "must_read_profile": True,
                            "must_search_bank": True,
                            "ask_only_missing_field": "floating_debt_rub",
                            "skipped_fields": state.skipped_fields,
                            "plan_format": "Use proposed_plan=[] for this small task unless a new research level is needed. level must be one of world,cis_ru,ru,industry,company,competitor,product_metric; never bank or news. Bank is a source, not a research level.",
                            "scenario": "If scenario_request is present, use exactly those inputs with calculate_scenario before analyze. "
                            "If floating debt is missing, first search company docs, then ask user. "
                            "After a skipped answer proceed with a qualitative conditional interpretation.",
                        },
                    }
                    step = await invoke(
                        "controller",
                        StepPlan,
                        payload,
                        controller_schema(allowed_tools, payload["allowed_decisions"]),
                    )
                    state.controller = ControllerOutput(
                        open_questions=[step.question],
                        proposed_plan=[
                            ResearchStep(
                                level="company",
                                question=step.question,
                                why_changes_decision="Закрывает текущий пробел перед клиентским выводом.",
                            )
                        ],
                        next=step.next,
                    )
                    state.plan = state.controller.proposed_plan
                    state.open_questions = state.controller.open_questions
                    decision = state.controller.next
                    if decision.kind == "tool":
                        if decision.action.tool not in allowed_tools:
                            policy_note("Выберите только инструмент из allowed_tools текущего шага.")
                            save()
                            continue
                        state.node = Node.TOOL_EXEC
                    elif decision.kind == "ask_user":
                        debt = feature(state, "floating_debt_rub")
                        if (
                            decision.field != "floating_debt_rub"
                            or debt
                            or not needs_debt_amount()
                            or decision.field in state.skipped_fields
                        ):
                            policy_note(
                                "Уточнение допускается только для действительно отсутствующей суммы плавающего долга."
                            )
                        elif "search_company_documents" not in state.observed_tools:
                            policy_note("Сначала проверьте доступные материалы компании инструментом.")
                        else:
                            wait_question(decision)
                            return
                    elif decision.kind == "analyze":
                        if not state.profile or "search_bank_claims" not in state.observed_tools:
                            policy_note(
                                "Перед анализом получить профиль и выполнить поиск банковских source claims."
                            )
                        elif expected_scenario(state, request) and not state.calculation_records:
                            policy_note(
                                "Выполните разрешённый сценарный расчёт по scenario_request перед анализом."
                            )
                        elif needs_debt_amount():
                            policy_note(
                                "Сумма плавающего долга отсутствует. Найдите её в материалах либо задайте вопрос."
                            )
                        else:
                            state.node = Node.ANALYZE
                    else:
                        policy_note(
                            "В этом сценарии ставка связана с долговой/оборотной экспозицией; объясните границы влияния вместо скрытия события."
                        )
                    save()
                elif state.node == Node.TOOL_EXEC:
                    if state.tool_calls >= self.settings.max_tools:
                        raise GatewayError("tool_call_limit")
                    call = state.controller.next.action
                    signature = hashlib.sha256(
                        (str(state.input_revision) + call.model_dump_json()).encode()
                    ).hexdigest()
                    if signature in state.seen_tool_calls:
                        policy_note(
                            "Идентичный запрос уже выполнялся; используйте результат или выберите другой пробел."
                        )
                        state.node = Node.PLAN
                        save()
                        continue
                    state.seen_tool_calls.append(signature)
                    state.tool_calls += 1
                    try:
                        observation = self.tools.run(call, state, request)
                        audit("tool_completed", observation.model_dump(mode="json"))
                    except ValueError as error:
                        observation = ToolObservation(
                            observation_id=new_id("obs"),
                            tool=call.tool,
                            outcome="blocked",
                            summary="Аргументы не соответствуют проверенным входам сценария.",
                            evidence_ids=[],
                            feature_ids=[],
                            calculation_ids=[],
                            error_code=str(error),
                        )
                        state.observations.append(observation.model_dump(mode="json"))
                        audit("tool_blocked", {"tool": call.tool, "code": str(error)})
                    state.node = Node.PLAN
                    save()
                elif state.node == Node.ANALYZE:
                    payload = {
                        "context": self.view(state, request).model_dump(mode="json"),
                        "previous_analysis": state.analysis.model_dump(mode="json")
                        if state.analysis
                        else None,
                        "deterministic_issues": state.gate_issues,
                        "instructions": "Return role/rule/action selections from role_catalogue and real bank_claim_ids. Do not return financial prose, numbers or source facts: server code owns those.",
                    }
                    payload["role_catalogue"] = catalogue(state.profile)
                    analysis = await invoke("analyst", AnalysisDraft, payload)
                    state.analysis = analysis
                    state.draft = assemble_core(analysis, state, self.view(state, request))
                    state.draft_id = new_id("draft")
                    state.node = Node.VERIFY
                    save()
                elif state.node == Node.VERIFY:
                    view = self.view(state, request)
                    issues = check_core(state.draft, state, view)
                    state.gate_issues = [i.model_dump(mode="json") for i in issues]
                    if issues:
                        state.verification = VerifierOutput(verdict="revise", issues=issues)
                    else:
                        state.verification = await invoke(
                            "verifier",
                            VerifierOutput,
                            {
                                "context": view.model_dump(mode="json"),
                                "draft": state.draft.model_dump(mode="json"),
                                "deterministic_checks": "passed",
                            },
                        )
                    from .verification import enforce_verification_policy

                    original_verdict = state.verification.verdict
                    state.verification = enforce_verification_policy(state.verification)
                    if state.verification.verdict != original_verdict:
                        audit(
                            "verification_policy",
                            {"model_verdict": original_verdict, "server_verdict": state.verification.verdict},
                        )
                    audit("verification", state.verification.model_dump(mode="json"))
                    known = {s.statement_id for s in state.draft.statements}
                    if any(not set(i.statement_ids) <= known for i in state.verification.issues):
                        raise GatewayError("verification_reference_invalid")
                    if state.verification.verdict == "pass":
                        state.verified_core_id = new_id("verified")
                        state.node = Node.PROJECT
                    else:
                        if state.replans >= self.settings.max_replans:
                            raise GatewayError("verification_replan_limit")
                        state.replans += 1
                        state.replan_tool_baseline = state.tool_calls
                        state.node = (
                            Node.PLAN if state.verification.verdict == "needs_evidence" else Node.ANALYZE
                        )
                        state.open_questions = [i.explanation for i in state.verification.issues][:8]
                        audit("replanned", {"reason": state.verification.verdict, "count": state.replans})
                    save()
                elif state.node == Node.PROJECT:
                    state.node = Node.PUBLISH
                    save()
                elif state.node == Node.PUBLISH:
                    if (
                        check_core(state.draft, state, self.view(state, request))
                        or not state.verified_core_id
                    ):
                        raise GatewayError("publication_gate_failed")
                    result = {
                        "mode": state.outcome_mode,
                        "core": state.draft.model_dump(mode="json"),
                        "role_views": project_roles(state.draft, state),
                        "calculations": state.calculation_records,
                        "verification": state.verification.model_dump(mode="json"),
                        "profile_version": state.profile_version,
                        "as_of": request["as_of"],
                        "corpus_metadata": self.corpus.metadata,
                        "company_context": self.corpus.company_metadata.get(
                            request["company_id"], {"source": "synthetic_demo"}
                        ),
                        "notice": "Автоматическая проверка структуры, источников и отдельный проход LLM. Не независимая экспертная валидация. Происхождение профиля указано отдельно.",
                    }
                    partial = (
                        bool(state.draft.missing_information) or state.draft.bank_coverage != "applicable"
                    )
                    state.status = RunStatus.PARTIAL if partial else RunStatus.SUCCEEDED
                    state.publish_outcome = "partial" if partial else "complete"
                    state.node = Node.FINISHED
                    save(
                        result,
                        {
                            "verified_core_id": state.verified_core_id,
                            "roles": [x["role"] for x in result["role_views"]],
                            "status": state.status.value,
                        },
                    )
                    return
                else:
                    raise GatewayError("invalid_state_transition")
        except Conflict:
            # Another authorized operation, e.g. cancellation, won the CAS.
            return
        except (GatewayError, AccessDenied) as error:
            code = error.code if isinstance(error, GatewayError) else str(error)
            state.last_error_code = code
            if isinstance(error, GatewayError) and error.retry_at:
                state.status = RunStatus.DEFERRED_BUDGET
                state.next_eligible_at = self.corpus.as_of(error.retry_at)
            else:
                state.status = RunStatus.FAILED
                state.node = Node.FINISHED
            audit("stopped", {"code": code, "status": state.status.value})
            try:
                save()
            except Conflict:
                pass
        except Exception as error:
            state.last_error_code = "internal_error"
            state.status = RunStatus.FAILED
            state.node = Node.FINISHED
            audit("internal_error", {"error_type": type(error).__name__})
            try:
                save()
            except Conflict:
                pass

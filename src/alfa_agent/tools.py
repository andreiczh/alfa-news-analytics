from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

from .corpus import AccessDenied, Corpus
from .domain import CalculationRecord, Citation, ToolObservation
from .settings import utcnow
from .state import RuntimeState
from .storage import new_id


def feature(state, name):
    if not state.profile:
        return None
    return next(
        (f for f in state.profile.features if f.name == name and f.status not in {"missing", "conflicting"}),
        None,
    )


def expected_scenario(state, request):
    debt = feature(state, "floating_debt_rub")
    rate_type = feature(state, "rate_type")
    if (
        not debt
        or not debt.numeric_value
        or debt.numeric_value.value is None
        or debt.numeric_value.value <= 0
    ):
        return None
    if debt.numeric_value.unit != "RUB":
        return None
    if not rate_type or rate_type.text_value != "floating":
        return None
    return {
        "formula": "floating_debt_rate_delta",
        "mode": "hypothetical",
        "principal_rub": str(debt.numeric_value.value),
        "delta_rate_pp": str(request["scenario_delta_pp"]),
        "horizon_days": request["horizon_days"],
        "day_count_basis": 365,
        "principal_refs": [{"kind": "feature", "ref_id": debt.feature_id}],
        "rate_refs": [],
        "assumptions": [
            "Условное изменение договорной ставки; не прогноз и не эффект новости.",
            "Остаток постоянен; полная переоценка ставки в начале горизонта; без хеджа, налогов и комиссий.",
        ],
    }


class ToolRunner:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus

    def run(self, call, state: RuntimeState, request):
        company = request["company_id"]
        as_of = self.corpus.as_of(request["as_of"])
        name = call.tool
        evidence, features, calculations = [], [], []
        summary = ""
        if name == "get_company_profile":
            state.profile = state.profile or self.corpus.profile(company)
            if state.profile.company_id != company:
                raise AccessDenied("company_not_accessible")
            state.profile_version = state.profile.version
            features = [f.feature_id for f in state.profile.features]
            evidence = list(
                dict.fromkeys(
                    c.ref_id for f in state.profile.features for c in f.source_refs if c.kind == "evidence"
                )
            )
            summary = "Профиль получен; missing/conflicting не являются подтверждёнными значениями."
        elif name == "search_bank_claims":
            claims = self.corpus.search(call.query, "bank", company, as_of, call.limit)
            for claim in claims:
                if claim.claim_id not in state.bank_claim_ids:
                    state.bank_claim_ids.append(claim.claim_id)
                evidence.extend(claim.evidence_ids)
            summary = (
                f"Найдено банковских source claims: {len(claims)}. Дата и авторство доступны в контексте."
            )
        elif name == "search_company_documents":
            docs = self.corpus.search(call.query, "document", company, as_of, call.limit * 2)
            docs = [doc for doc in docs if doc.tenant_company_id == company][: call.limit]
            evidence = [
                e.evidence_id
                for e in self.corpus.evidence.values()
                if e.document_id in {d.document_id for d in docs}
            ]
            summary = (
                f"Найдено клиентских документов: {len(docs)}; документ может не содержать искомого значения."
            )
        elif name == "search_events":
            from datetime import timedelta

            docs = self.corpus.search(call.query, "document", company, as_of, call.limit * 2)
            docs = [
                doc
                for doc in docs
                if doc.source_kind == "news"
                and (
                    doc.published_at is None
                    or doc.published_at >= (as_of - timedelta(days=call.lookback_days)).date()
                )
            ][: call.limit]
            evidence = [
                e.evidence_id
                for e in self.corpus.evidence.values()
                if e.document_id in {d.document_id for d in docs}
            ]
            summary = f"Найдено новостных документов: {len(docs)}."
        elif name == "read_evidence":
            for item in call.evidence_ids:
                if item in {e.evidence_id for e in state.answer_evidence}:
                    evidence.append(item)
                else:
                    self.corpus.read_evidence([item], company, as_of)
                    evidence.append(item)
            summary = "Прочитаны разрешённые свидетельства."
        elif name == "calculate_scenario":
            expected = expected_scenario(state, request)
            if expected is None:
                raise ValueError("floating_exposure_missing")
            inputs = call.inputs
            if (
                inputs.principal_rub != Decimal(expected["principal_rub"])
                or inputs.delta_rate_pp != Decimal(expected["delta_rate_pp"])
                or inputs.horizon_days != expected["horizon_days"]
                or inputs.day_count_basis != 365
                or inputs.mode != "hypothetical"
                or inputs.principal_refs != [Citation.model_validate(x) for x in expected["principal_refs"]]
                or inputs.rate_refs
            ):
                raise ValueError("scenario_inputs_do_not_match_authorized_request")
            inputs = inputs.model_copy(update={"assumptions": expected["assumptions"]})
            value = (
                inputs.principal_rub
                * inputs.delta_rate_pp
                / Decimal(100)
                * inputs.horizon_days
                / inputs.day_count_basis
            )
            record = CalculationRecord(
                calculation_id=new_id("calc"),
                formula_version="floating_v1",
                inputs=inputs,
                result_rub=value,
                rounding="decimal_half_even_2dp_for_display",
                created_at=utcnow(),
                interpretation="Условная чувствительность процентного расхода, не фактический эффект новости. "
                + str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
                + " RUB",
            )
            state.calculation_records.append(record.model_dump(mode="json"))
            state.calculation_ids.append(record.calculation_id)
            calculations.append(record.calculation_id)
            summary = "Сценарный расчёт выполнен кодом. Условный шаг ставки не является прогнозом банка."
        else:
            raise ValueError("tool_not_allowed")
        for evidence_id in evidence:
            if evidence_id not in state.evidence_ids:
                state.evidence_ids.append(evidence_id)
        if name not in state.observed_tools:
            state.observed_tools.append(name)
        observation = ToolObservation(
            observation_id=new_id("obs"),
            tool=name,
            outcome="ok" if evidence or features or calculations else "empty",
            summary=summary,
            evidence_ids=evidence,
            feature_ids=features,
            calculation_ids=calculations,
        )
        state.observations.append(observation.model_dump(mode="json"))
        return observation

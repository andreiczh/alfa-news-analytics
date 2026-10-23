from __future__ import annotations

from decimal import Decimal

ROLES = {
    "cfo": (
        "CFO — финансовый директор",
        "Бюджет и чувствительность процентных расходов",
        ["floating_debt_rub", "capex_planned_rub"],
    ),
    "treasurer": (
        "Руководитель казначейства",
        "Ликвидность, договорная ставка и даты переоценки",
        ["floating_debt_rub", "rate_type"],
    ),
    "ceo": (
        "CEO — генеральный директор",
        "Сценарии инвестиций и условия принятия решения",
        ["capex_planned_rub"],
    ),
    "coo": (
        "COO — операционный директор",
        "Оборотный капитал и проверка политики запасов",
        ["inventory_days"],
    ),
    "cco": (
        "CCO — коммерческий директор",
        "Отсрочки покупателям и возврат денежных средств",
        ["receivables_days", "credit_sales_share"],
    ),
}


def project_roles(core, state):
    """Select verified content and format source-backed metrics, no extra LLM."""
    result = []
    for role, (label, focus, names) in ROLES.items():
        selected_actions = [a for a in core.actions if role in a.target_roles]
        selected_ids = {sid for a in selected_actions for sid in a.rationale_statement_ids}
        selected_ids.update(s.statement_id for s in core.statements if s.derivation == "quoted_source")
        if role in {"cfo", "treasurer"}:
            selected_ids.update(s.statement_id for s in core.statements if s.derivation == "calculated")
        selected = [s.model_dump(mode="json") for s in core.statements if s.statement_id in selected_ids]
        metrics = []
        if state.profile:
            present = {f.name for f in state.profile.features if f.status not in {"missing", "conflicting"}}
            fallbacks = {
                "cfo": ["has_loan", "has_deposit", "revenue_source_units"],
                "treasurer": ["has_loan", "has_deposit", "has_guarantee"],
                "ceo": ["industry", "business_stage"],
                "coo": ["sample_outgoing_counterparties", "industry", "employees_band"],
                "cco": [
                    "sample_incoming_top_share",
                    "sample_incoming_counterparties",
                    "has_factoring",
                    "industry",
                ],
            }
            selected_names = names if any(n in present for n in names) else fallbacks[role]
            for f in state.profile.features:
                if f.name not in selected_names or f.status in {"missing", "conflicting"}:
                    continue
                value = (
                    f.numeric_value.value
                    if f.numeric_value
                    else f.text_value
                    if f.text_value is not None
                    else f.boolean_value
                )
                metrics.append(
                    {
                        "name": f.name,
                        "value": str(value),
                        "unit": f.numeric_value.unit if f.numeric_value else None,
                        "ref_id": f.feature_id,
                        "provenance": f.status,
                    }
                )
        scenario = None
        if state.calculation_records and role in {"cfo", "treasurer"}:
            c = state.calculation_records[-1]
            scenario = {
                "calculation_id": c["calculation_id"],
                "amount_rub": str(Decimal(c["result_rub"]).quantize(Decimal(".01"))),
                "delta_rate_pp": c["inputs"]["delta_rate_pp"],
                "horizon_days": c["inputs"]["horizon_days"],
                "day_count_basis": c["inputs"]["day_count_basis"],
                "label": "Условное изменение процентного расхода; не эффект сохранения ставки и не прогноз",
                "assumptions": c["inputs"]["assumptions"],
            }
        result.append(
            {
                "role": role,
                "label": label,
                "headline": core.headline,
                "focus": focus,
                "statements": selected,
                "actions": [a.model_dump(mode="json") for a in selected_actions],
                "metrics": metrics,
                "scenario": scenario,
                "limitations": core.limitations,
                "empty_actions_note": None
                if selected_actions
                else "Источник не обосновывает отдельное действие для этой роли.",
            }
        )
    return result

from decimal import Decimal
from .domain import InsightCore
from .role_rules import render_rule


def assemble_core(analysis, state, view):
    """Source statements and calculated numbers are copied by code."""
    news = next(e for e in view.evidence if e.evidence_id == "news_rate_fact")
    statements = [
        {
            "statement_id": "source_event",
            "text": news.quote,
            "claim_type": "FACT",
            "owner": {"kind": "external", "name": "Пресс-служба Банка России"},
            "derivation": "quoted_source",
            "time_scope": {"raw": "Событие 11 сентября 2026", "start": "2026-09-11", "end": "2026-09-11"},
            "conditions": [],
            "citations": [{"kind": "evidence", "ref_id": news.evidence_id}],
        }
    ]
    used = list(dict.fromkeys(cid for role in analysis.roles for cid in role.bank_claim_ids))
    known = {c.claim_id: c for c in view.bank_claims}
    for cid in used:
        claim = known.get(cid)
        if not claim:
            continue
        statements.append(
            {
                "statement_id": "source_" + cid,
                "text": claim.text,
                "claim_type": claim.claim_type,
                "owner": claim.owner.model_dump(mode="json"),
                "derivation": "quoted_source",
                "time_scope": claim.time_scope.model_dump(mode="json"),
                "conditions": claim.conditions,
                "citations": [{"kind": "evidence", "ref_id": eid} for eid in claim.evidence_ids],
            }
        )
    for calculation in view.calculations:
        amount = calculation.result_rub.quantize(Decimal(".01"))
        statements.append(
            {
                "statement_id": "sensitivity",
                "text": f"Условное изменение процентного расхода: {amount} руб. при шаге договорной ставки {calculation.inputs.delta_rate_pp} п.п. за {calculation.inputs.horizon_days} дней.",
                "claim_type": "SCENARIO",
                "owner": {"kind": "model", "name": "Сценарный расчёт сервиса"},
                "derivation": "calculated",
                "time_scope": {},
                "conditions": calculation.inputs.assumptions,
                "citations": [{"kind": "calculation", "ref_id": calculation.calculation_id}],
            }
        )
    actions = []
    for role in analysis.roles:
        rule = render_rule(role, state.profile)
        citations = [{"kind": "feature", "ref_id": fid} for fid in rule["feature_ids"]]
        for cid in role.bank_claim_ids:
            if cid in known:
                citations.extend({"kind": "evidence", "ref_id": eid} for eid in known[cid].evidence_ids)
        statements.append(
            {
                "statement_id": "ai_" + role.role,
                "text": rule["explanation"],
                "claim_type": "SCENARIO",
                "owner": {"kind": "model", "name": "Интерпретация сервиса"},
                "derivation": "conditional",
                "time_scope": {},
                "conditions": rule["conditions"],
                "citations": citations,
            }
        )
        actions.append(
            {
                "title": rule["action_title"],
                "rationale_statement_ids": ["ai_" + role.role],
                "target_roles": [role.role],
                "required_data": rule["required_data"],
            }
        )
    missing = []
    rate = next(
        (
            f.text_value
            for f in state.profile.features
            if f.name == "rate_type" and f.status not in {"missing", "conflicting"}
        ),
        None,
    )
    if (
        rate == "floating"
        and state.profile
        and not any(
            f.name == "floating_debt_rub" and f.status not in {"missing", "conflicting"}
            for f in state.profile.features
        )
    ):
        missing.append("Остаток плавающего долга не сообщён; численная чувствительность не рассчитана.")
    if rate not in {"fixed", "floating", "none"}:
        missing.append("Тип и условия долговой ставки не установлены; денежный эффект не рассчитан.")
    if state.profile.unresolved_identity:
        missing.append(
            "В исходном наборе конфликтуют строки одного ID компании; идентичность требует согласования."
        )
    source = "synthetic_demo"
    if any(d.source_kind == "case_csv" for d in view.documents):
        source = "case_csv"
    elif any(
        d.source_kind in {"company_upload", "user_answer"} for d in view.documents
    ) and not state.profile.company_id.startswith("demo_"):
        source = "company_upload"
    provenance_note = {
        "synthetic_demo": "Исходный профиль демонстрационной компании синтетический.",
        "case_csv": "Профиль включает обезличенные данные организаторов. Суммы и условия долговых продуктов из флагов не восстановлены.",
        "company_upload": "Профиль собран из сведений пользователя и загруженных документов; юридическая идентичность и показатели не проверены независимо.",
    }[source]
    return InsightCore.model_validate(
        {
            "event_ids": view.triggering_event_ids,
            "headline": "Ставка без изменений: условия финансирования и оборотный капитал",
            "statements": statements,
            "impact": {
                "direction": analysis.direction,
                "materiality": "unknown",
                "evidence_strength": "conditional",
                "urgency": analysis.urgency,
                "mechanism_statement_ids": ["ai_" + r.role for r in analysis.roles],
                "exposed_feature_ids": list(
                    dict.fromkeys(
                        fid for r in analysis.roles for fid in render_rule(r, state.profile)["feature_ids"]
                    )
                ),
                "horizon": {},
            },
            "actions": actions,
            "bank_claim_ids_used": used,
            "bank_coverage": "applicable" if used else "not_found",
            "limitations": list(
                dict.fromkeys(
                    [
                        provenance_note,
                        "Относительная существенность не оценена: нет EBITDA/прибыли и подтверждённого порога.",
                        "Позиция банка относится к обзору от 11 сентября 2026; это не подтверждение его самой свежей позиции.",
                        "Расчёт — условная линейная чувствительность, не фактическая экономия от сохранения ставки.",
                        "Взяты выборочные фрагменты страницы 1; полная разметка PDF и человеческая проверка не выполнены.",
                    ]
                )
            ),
            "missing_information": missing,
        }
    )

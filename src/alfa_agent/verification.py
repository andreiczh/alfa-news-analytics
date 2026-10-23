from __future__ import annotations

from decimal import Decimal
import re

from .domain import InsightCore, VerificationIssue, VerifierOutput


def enforce_verification_policy(output):
    strict = {"unsupported", "wrong_owner", "wrong_time", "wrong_number", "contradiction"}
    issues = [i.model_copy(update={"severity": "blocking"}) if i.code in strict else i for i in output.issues]
    verdict = output.verdict
    if verdict == "pass" and any(i.severity == "blocking" for i in issues):
        verdict = "revise"
    return VerifierOutput(verdict=verdict, issues=issues)


def numbers(text):
    result = []
    # A date is not a decimal amount: 11.09.2026 must not become 11.09.
    date_pattern = r"\b(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})\b"
    for match in re.finditer(date_pattern, text):
        result.extend((Decimal(x), Decimal(1)) for x in re.split(r"[.-]", match.group()))
    text = re.sub(date_pattern, " ", text)
    for match in re.finditer(r"(?<![\w])[-+−]?\d[\d \u00a0\u202f]*(?:[.,]\d+)?", text):
        raw = re.sub(r"[ \u00a0\u202f]", "", match.group()).replace("−", "-").replace(",", ".")
        suffix = text[match.end() :].lstrip().lower()
        scale = Decimal(
            10**9
            if suffix.startswith(("млрд", "миллиард"))
            else 10**6
            if suffix.startswith(("млн", "миллион"))
            else 1000
            if suffix.startswith(("тыс", "тысяч"))
            else 1
        )
        result.append((Decimal(raw) * scale, scale))
    return result


def check_core(core: InsightCore, state, view):
    issues = []
    evidence = {e.evidence_id: e for e in view.evidence}
    features = {f.feature_id: f for f in view.profile.features} if view.profile else {}
    calculations = {c.calculation_id: c for c in view.calculations}
    docs = {d.document_id: d for d in view.documents}
    claims = {c.claim_id: c for c in view.bank_claims}

    def block(code, ids, explanation, fix):
        issues.append(
            VerificationIssue(
                severity="blocking", code=code, statement_ids=ids, explanation=explanation, proposed_fix=fix
            )
        )

    if not set(core.event_ids) <= set(view.triggering_event_ids):
        block(
            "unsupported",
            [],
            "Выбранное событие не относится к входу задания.",
            "Использовать trigger event IDs.",
        )
    if not set(core.impact.exposed_feature_ids) <= features.keys():
        block(
            "unsupported",
            [],
            "Неизвестный признак компании.",
            "Использовать только признаки текущего профиля.",
        )
    if not set(core.bank_claim_ids_used) <= claims.keys():
        block(
            "unsupported", [], "Неизвестный source claim банка.", "Повторно получить источник инструментом."
        )
    if core.bank_coverage == "applicable" and not core.bank_claim_ids_used:
        block(
            "unsupported", [], "Нет применённого содержания банка.", "Указать реальный claim или ограничение."
        )
    bank_evidence_ids = {
        eid for cid in core.bank_claim_ids_used if cid in claims for eid in claims[cid].evidence_ids
    }
    if core.bank_coverage == "applicable" and not any(
        s.owner.kind == "model"
        and any(c.kind == "evidence" and c.ref_id in bank_evidence_ids for c in s.citations)
        and any(c.kind == "feature" and c.ref_id in features for c in s.citations)
        for s in core.statements
    ):
        block(
            "unsupported",
            [],
            "Банк процитирован отдельно, но его вклад в клиентский вывод не показан.",
            "Обосновать хотя бы одну AI-интерпретацию одновременно банковским evidence и признаком компании.",
        )
    event_date = next((e.published_at for e in view.events if e.event_id in core.event_ids), None)
    for cid in core.bank_claim_ids_used:
        claim = claims.get(cid)
        if not claim:
            continue
        doc = docs[claim.document_id]
        if core.bank_coverage == "applicable" and (
            doc.published_at is None
            or (claim.time_scope.end and event_date and claim.time_scope.end < event_date)
        ):
            block(
                "wrong_time",
                [],
                "Исторический/недатированный источник назван применимой позицией.",
                "Указать historical_only/unresolved и ограничение.",
            )

    for statement in core.statements:
        ids = [statement.statement_id]
        allowed = set()
        cited_claims = []
        for citation in statement.citations:
            if citation.kind == "evidence":
                item = evidence.get(citation.ref_id)
                if item:
                    allowed.update(n for n, _ in numbers(item.quote or ""))
                    doc = docs[item.document_id]
                    if doc.published_at:
                        allowed.update(
                            map(
                                Decimal, (doc.published_at.year, doc.published_at.month, doc.published_at.day)
                            )
                        )
                    cited_claims.extend(c for c in claims.values() if item.evidence_id in c.evidence_ids)
                else:
                    block(
                        "unsupported",
                        ids,
                        "Цитата не получена или недоступна.",
                        "Получить разрешённое evidence.",
                    )
            elif citation.kind == "feature":
                item = features.get(citation.ref_id)
                if not item or item.status in {"missing", "conflicting"}:
                    block(
                        "missing_data",
                        ids,
                        "Недоступный/неразрешённый признак использован как значение.",
                        "Уточнить данные или сделать вывод условным.",
                    )
                elif item.numeric_value:
                    value = item.numeric_value
                    allowed.update(x for x in (value.value, value.minimum, value.maximum) if x is not None)
                    allowed.update(n for n, _ in numbers(value.raw))
                    if value.unit == "fraction" and value.value is not None:
                        allowed.add(value.value * 100)
            elif citation.kind == "calculation":
                item = calculations.get(citation.ref_id)
                if not item:
                    block("wrong_number", ids, "Расчёт не выполнялся.", "Вызвать calculate_scenario.")
                else:
                    allowed.update(
                        [
                            item.inputs.principal_rub,
                            item.inputs.delta_rate_pp,
                            abs(item.inputs.delta_rate_pp),
                            Decimal(item.inputs.horizon_days),
                            Decimal(item.inputs.day_count_basis),
                            item.result_rub,
                            abs(item.result_rub),
                            item.result_rub.quantize(Decimal(".01")),
                            abs(item.result_rub).quantize(Decimal(".01")),
                        ]
                    )
        if statement.derivation == "calculated" and not any(
            c.kind == "calculation" for c in statement.citations
        ):
            block(
                "wrong_number", ids, "Численный вывод без расчёта.", "Сослаться на реальный calculation ID."
            )
        if statement.derivation == "conditional" and not statement.conditions:
            block("unsupported", ids, "Условный вывод не содержит условий.", "Указать предпосылки явно.")
        if statement.owner.kind == "bank":
            matching = [
                c for c in cited_claims if c.owner.kind == "bank" and c.claim_id in core.bank_claim_ids_used
            ]
            if not matching:
                block(
                    "wrong_owner",
                    ids,
                    "Банку приписан вывод без банковского source claim.",
                    "Разделить позицию банка и AI-интерпретацию.",
                )
            elif not any(c.claim_type == statement.claim_type for c in matching):
                block(
                    "wrong_time",
                    ids,
                    "Тип позиции банка изменён, например FORECAST в FACT.",
                    "Сохранить исходный claim_type.",
                )
        for number, scale in numbers(statement.text):
            matches = number in allowed
            if not matches and scale > 1 and any(c.kind == "calculation" for c in statement.citations):
                matches = any(abs(number - n) <= scale / 200 for n in allowed)
            if not matches:
                block(
                    "wrong_number",
                    ids,
                    "В утверждении есть число без подтверждения его ссылками.",
                    "Убрать число либо привести источник/расчёт с корректными единицами.",
                )
                break
    return issues

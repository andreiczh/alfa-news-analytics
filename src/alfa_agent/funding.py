"""Company-owned debt inputs and reproducible contractual-rate sensitivity.

This is a gross interest-expense scenario, not a forecast, NPV, bank ALM or a
calculation of a news event's realized effect. Unknowns never become zero.
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .analysis_policy import planning_policy


class DebtTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rate_type: Literal["floating", "fixed", "unknown"] = "unknown"
    facility_name: str = Field(default="Договор для сценария", min_length=2, max_length=120)
    principal_rub: Decimal | None = Field(default=None, gt=0, le=Decimal("1e15"))
    currency: Literal["RUB", "USD", "EUR", "CNY", "other"] = "RUB"
    benchmark: Literal["key_rate", "ruonia", "other", "unknown"] = "unknown"
    repricing_at: date | None = None
    maturity_at: date | None = None
    fixed_until: date | None = None
    constant_principal_confirmed: bool = False
    no_hedge_confirmed: bool = False
    no_rate_review_confirmed: bool = False
    synthetic: bool = False


class FundingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    terms: DebtTerms


def debt_terms(snapshot):
    field = snapshot.get("context", {}).get("fields", {}).get("debt_contract")
    if not field:
        return DebtTerms(), None
    try:
        value = field["value"]
        if value.startswith("Синтетические данные.\n"):
            value = value.split("\n", 1)[1]
        return DebtTerms.model_validate_json(value), "debt_contract"
    except (ValueError, TypeError):
        return DebtTerms(), None


def funding_analysis(snapshot, role, as_of, days=90, bank_company=False):
    terms, key = debt_terms(snapshot)
    policy = planning_policy(as_of, days)
    start, end = date.fromisoformat(policy["start"]), date.fromisoformat(policy["end"])
    missing = []
    result = {
        "version": "funding-v1",
        "rate_type": terms.rate_type,
        "status": "insufficient_data",
        "context_key": key,
        "synthetic": terms.synthetic,
        "terms": terms.model_dump(mode="json"),
        "planning_policy": policy,
        "scenarios": [],
        "missing_data": missing,
        "formula": "principal_RUB × contractual_rate_delta_pp / 100 × exposure_days / 365",
        "assumptions": [
            "Условное изменение именно договорной ставки, не прогноз ключевой ставки.",
            "Постоянный остаток, без хеджа, комиссий, налогов и расчёта чистой прибыли.",
            "Простой процент ACT/365; начисление со дня переоценки до погашения (день погашения исключён).",
        ],
        "net_company_effect": "not_calculated",
        "event_effect": "not_established",
        "role": role,
        "human_expert_verified": False,
    }
    if bank_company:
        missing.append(
            "Для банка нужна ALM-модель активов и обязательств; корпоративная формула неприменима."
        )
        result["status"] = "bank_alm_required"
    elif terms.currency != "RUB":
        missing.append("Рублёвый калькулятор не рассчитывает валютный долг и эффект конвертации")
    elif terms.rate_type == "unknown":
        missing.extend(
            ["Тип ставки и формула договора", "Остаток и валюта долга", "Дата переоценки и погашения"]
        )
    elif terms.rate_type == "fixed":
        result["assumptions"][0] = (
            "Стресс рыночной ставки не переоценивает подтверждённую фиксированную договорную ставку; её изменение равно нулю только в защищённом периоде."
        )
        if not terms.fixed_until or not terms.maturity_at or not terms.no_rate_review_confirmed:
            missing.append("Срок фиксации, погашение и подтверждение отсутствия договорного пересмотра")
        else:
            protected_end = min(end, terms.fixed_until, terms.maturity_at - timedelta(days=1))
            if protected_end >= start:
                result["status"] = "fixed_protected" if protected_end == end else "fixed_partial_window"
                result["protected_end"] = protected_end.isoformat()
                result["scenarios"] = [
                    {
                        "scenario_delta_pp": str(delta),
                        "shock_basis": "market_rate_change",
                        "delta_contract_rate_pp": "0",
                        "gross_interest_change_rub": "0.00",
                        "exposure_days": (protected_end - start).days + 1,
                        "scope": "Только действующий фиксированный договор до конца защищённого периода",
                    }
                    for delta in (-1, 0, 1)
                ]
                if protected_end < end:
                    missing.append(
                        "Условия нового привлечения/рефинансирования после окончания защищённого периода"
                    )
            else:
                missing.append("Фиксация уже не покрывает начало планового периода")
    else:
        if terms.currency != "RUB":
            missing.append("Рублёвый калькулятор не рассчитывает валютный долг")
        if terms.principal_rub is None:
            missing.append("Положительный остаток долга в RUB")
        if terms.benchmark == "unknown":
            missing.append("Бенчмарк и формула договорной ставки")
        if not terms.repricing_at:
            missing.append("Дата договорной переоценки")
        if not terms.maturity_at:
            missing.append("Дата погашения")
        if not terms.constant_principal_confirmed:
            missing.append("Подтверждение постоянного остатка в расчётном периоде")
        if not terms.no_hedge_confirmed:
            missing.append("Подтверждение отсутствия хеджа для этого сценария")
        if terms.repricing_at and terms.maturity_at and terms.repricing_at >= terms.maturity_at:
            missing.append("Дата переоценки должна предшествовать погашению")
        if not missing:
            effective = max(start, terms.repricing_at)
            stop = min(end, terms.maturity_at - timedelta(days=1))
            exposure = max(0, (stop - effective).days + 1)
            result.update(
                status="sensitivity_available",
                effective_start=effective.isoformat() if exposure else None,
                effective_end=stop.isoformat() if exposure else None,
            )
            if exposure == 0:
                result["zero_reason"] = (
                    "Договор погашен до начала окна; ноль относится только к этому договору, условия рефинансирования неизвестны."
                    if terms.maturity_at <= start
                    else "Дата переоценки находится за пределами окна начисления; ноль относится только к выбранному сценарию внутри этого окна."
                )
            if terms.maturity_at <= end:
                missing.append("Условия нового привлечения/рефинансирования после погашения договора")
            for delta in (-1, 0, 1):
                amount = (terms.principal_rub * Decimal(delta) / 100 * exposure / 365).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_EVEN
                )
                result["scenarios"].append(
                    {
                        "delta_contract_rate_pp": str(delta),
                        "scenario_delta_pp": str(delta),
                        "shock_basis": "contractual_rate_change",
                        "gross_interest_change_rub": str(amount),
                        "exposure_days": exposure,
                        "scope": "Гипотетическая чувствительность процентных расходов по указанному договору",
                    }
                )
    result["next_action"] = (
        (
            "Сопоставьте чувствительность с бюджетом процентных расходов; отдельно оцените новое привлечение и комиссии."
            if role == "cfo"
            else "Сверьте даты переоценки и погашения с платёжным календарём, остатками и доступными кредитными линиями."
        )
        if not missing
        else (
            "Для проверки бюджета процентных расходов CFO запросите: "
            if role == "cfo"
            else "Для платёжного календаря и плана фондирования казначея запросите: "
        )
        + "; ".join(missing)
        + "."
    )
    return result


def terms_json(terms):
    return ("Синтетические данные.\n" if terms.synthetic else "") + json.dumps(
        terms.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )


def news_lens(source):
    """Classify what a read primary publication can establish, without a rate estimate."""
    import re

    title = source.get("title", "")
    if re.search(r"сохран\w*[^.!?]{0,70}ключев\w*\s+ставк", title, re.I):
        kind, text = (
            "rate_pause",
            "Решение о сохранении ставки не объявляет нового изменения ключевой ставки. Переоценка старых договоров и изменения спредов проверяются отдельно.",
        )
    elif re.search(r"сниз\w*[^.!?]{0,70}ключев\w*\s+ставк", title, re.I):
        kind, text = (
            "rate_cut",
            "Снижение ключевой ставки относится к дате источника. Его перенос в договор зависит от бенчмарка, формулы и даты переоценки; для фиксированной ставки автоматический перенос не предполагается.",
        )
    elif re.search(r"мониторинг\w*.*максимальн\w*.*став", title, re.I):
        kind, text = (
            "deposit_monitoring",
            "Показатель розничных вкладов обследуемой группы банков не является ставкой корпоративного кредита или предложением размещения средств компании. Запросите сопоставимые корпоративные условия.",
        )
    elif re.search(r"Заявление Председателя|прогноз", title, re.I):
        kind, text = (
            "policy_outlook",
            "Заявление и прогноз задают предпосылки стресс-проверки, но не гарантируют будущие решения регулятора и не меняют договор автоматически.",
        )
    else:
        kind, text = (
            "financial_context",
            "Прочитанная публикация требует проверки связи с конкретным договором; фактическое изменение процентных расходов не установлено.",
        )
    return {
        "category": kind,
        "source_id": source["source_id"],
        "published_at": source.get("published_at"),
        "interpretation": text,
        "actual_contract_rate_change_pp": None,
    }


def funding_memo(ledger, sources, request, snapshot):
    """A bounded corporate financing explanation; no generated monetary figures."""
    import re
    from urllib.parse import urlsplit
    from .narrative import DecisionNarrative, NarrativePoint, source_message_options

    if request["bank_company"] or request["role"] not in {"cfo", "treasurer"}:
        return None
    if not re.search(r"ставк|фондир|финансир|долг|кредит|ликвид|инфляц", request["question"], re.I):
        return None
    terms, key = debt_terms(snapshot)
    source_ids = {
        s["source_id"]
        for s in sources
        if s["kind"] == "public_page"
        and (
            (urlsplit(s.get("url", "")).hostname or "").lower() == "cbr.ru"
            or (urlsplit(s.get("url", "")).hostname or "").lower().endswith(".cbr.ru")
        )
    }
    selected = request.get("news_item_id")
    event = next(
        (
            o
            for o in source_message_options(ledger)
            if any(
                a["source_id"] in source_ids
                and a["anchor_id"] == o["anchor_id"]
                and (not selected or a["source_id"] == selected)
                for a in ledger
            )
        ),
        None,
    )
    wanted = "curated_bank_case_06_2_16" if terms.rate_type == "floating" else "curated_bank_case_06_2_13"
    bank = next((a for a in ledger if a["source_id"] == wanted), None)
    company = next((a for a in ledger if a.get("context_key") == key and key), None)
    if not event or not bank or not company:
        return None
    refs = [event["anchor_id"], bank["anchor_id"], company["anchor_id"]]
    event_source = next(
        s
        for s in sources
        if any(a["source_id"] == s["source_id"] and a["anchor_id"] == event["anchor_id"] for a in ledger)
    )
    lens = news_lens(event_source)
    titles = {
        "floating": "Плавающий долг: ставка и сроки переоценки",
        "fixed": "Фиксированный долг: защита и рефинансирование",
        "unknown": "Условия долга: сначала подтвердить договор",
    }
    summaries = {
        "floating": "Для указанного плавающего договора важны бенчмарк и дата переоценки. Чувствительность ниже относится к условному изменению договорной ставки; фактический эффект новости и чистый результат компании не установлены.",
        "fixed": "Фиксированная ставка защищает действующий договор только в подтверждённых пределах фиксации. Новое привлечение и рефинансирование нужно оценивать отдельно; вывод о всей стоимости финансирования компании не установлен.",
        "unknown": "Без типа ставки и условий договора нельзя установить направление и размер изменения процентных расходов. Сначала подтвердите договор; отсутствие данных не означает нулевой эффект.",
    }
    condition = "При подтверждении условий конкретного договора; обзор банка описывает рынок на дату документа, а не условия компании."

    def point(text, anchors=refs):
        return NarrativePoint(kind="interpretation", text=text, anchor_ids=anchors, condition=condition)

    action = funding_analysis(snapshot, request["role"], request["as_of"], request.get("planning_days", 30))[
        "next_action"
    ]
    if len(action) > 650:
        action = (
            "Подтвердите тип и формулу ставки, остаток, дату переоценки, погашение и условия хеджирования."
        )
    return DecisionNarrative(
        title=titles[terms.rate_type],
        main_conclusion=point(summaries[terms.rate_type]).model_dump(),
        situation=[
            {
                "kind": event["kind"],
                "text": event["text"],
                "anchor_ids": [event["anchor_id"]],
                "condition": "Позиция первоисточника на дату публикации; не гарантия будущих условий договора.",
            }
        ],
        drivers=[
            point(lens["interpretation"], [event["anchor_id"]]),
            point(
                "Банк различает фиксированный и переменный купон. Это основание отдельно проверить формат действующего долга и условия нового финансирования; оценка рынка из обзора не заменяет договор."
            ),
        ],
        company_impact=[point(summaries[terms.rate_type])],
        risks=[
            point(
                "Рефинансирование, изменение спреда и сроки денежных потоков могут отличаться от сценария договорной ставки; достаточность ликвидности не установлена."
            )
        ],
        opportunities=[
            point(
                "При наличии сопоставимых предложений можно проверить альтернативные сроки и структуру финансирования, учитывая комиссии и ограничения договора."
            )
        ],
        recommendations=[point(action)],
        forecast_status="insufficient_evidence",
        forecast_limit="Расчёт чувствительности не является прогнозом ставки, инвестиционной рекомендацией или оценкой прибыли. Направление будущих решений регулятора не установлено.",
        scenarios=[],
        context_keys=[key],
    )

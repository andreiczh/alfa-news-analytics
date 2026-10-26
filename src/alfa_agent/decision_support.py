"""Source-aware first reading. Conditional channels are not factual financial effects."""

from __future__ import annotations

import re
from .research_rules import RULES
from .personalization import ROLE_NAMES

TOPIC_MECHANISMS = {
    "rates": ("bank_alm", "corporate_funding"),
    "funding": ("bank_liquidity", "corporate_funding"),
    "capital": ("regulation", "regulation"),
    "credit": ("bank_credit_risk", "cash_cycle"),
    "earnings": ("bank_credit_risk", "demand"),
    "fx": ("fx", "fx"),
    "inflation": ("costs", "costs"),
    "operations": ("costs", "costs"),
    "demand": ("demand", "demand"),
    "products": ("competition", "competition"),
    "strategy": ("competition", "competition"),
    "brand": ("competition", "competition"),
}
ROLE_QUESTIONS = {
    "cfo": "Сопоставьте фактор с бюджетом, маржой и подтверждёнными финансовыми условиями.",
    "treasurer": "Проверьте сроки поступлений, платежей и переоценки источников ликвидности.",
    "ceo": "Сопоставьте событие со стратегическими целями и условиями инвестиционных решений.",
    "coo": "Проверьте затронутые договоры, процессы закупок и операционные сроки.",
    "cco": "Проверьте спрос, цены и условия продаж в целевых сегментах.",
    "cmo": "Проверьте изменения поведения целевых сегментов, конкурентов и эффективности каналов.",
    "other": "Проверьте, есть ли у компании зависимость от описанного фактора.",
}


def news_interpretation(item, context, role):
    topics = item.get("topics") or []
    topic = item.get("primary_topic") or next((t for t in topics if t in TOPIC_MECHANISMS), None)
    mechanism = TOPIC_MECHANISMS[topic][0 if context.get("bank") else 1] if topic else None
    rule = RULES.get(mechanism)
    read = item.get("read_status") == "article_read"
    why = ". ".join(dict.fromkeys(r.strip(" .") for r in item.get("reasons", [])[:2]))
    if why:
        why += "."
    if item.get("matched_priorities") and not any("приоритет" in r.casefold() for r in item.get("reasons", [])[:2]):
        why += " Связано с вашим приоритетом: «" + item["matched_priorities"][0] + "»."
    needs = rule["needs"] if rule else []
    relevant = [f for f in context.get("company_factors", []) if topic in f["topics"]]
    need_keys = {key for key, _ in needs}
    relevant.sort(key=lambda f: (f["key"] in need_keys, f["origin"] == "company_profile"), reverse=True)
    factor = relevant[0] if relevant else None
    available = {f["key"] for f in relevant}
    missing = [label for key, label in needs if key not in available]
    conflicts = [f["label"] for f in context.get("factor_conflicts", []) if topic in f["topics"]]
    role_action = ROLE_QUESTIONS.get(role, ROLE_QUESTIONS["other"])
    if not read:
        impact = "Текст источника пока не прочитан. Обоснованное влияние по одному заголовку не определено."
        condition = "Откройте первоисточник или запустите исследование для проверки сообщения."
    elif rule:
        impact, condition = rule["effect"], rule["condition"]
    else:
        impact = "По доступному тексту канал влияния на компанию не установлен."
        condition = "Нужно уточнить связь события с деятельностью и приоритетами компании."
    pause_report = read and topic == "rates" and bool(re.search(r"\b(?:сохранил\w*|оставил\w*)[^.!?]{0,90}ключев\w*\s+ставк", item["title"], re.I)) and not re.search(r"не\s+(?:сохранил|оставил)|прогноз|ожида|планир|может|намерен", item["title"], re.I)
    if pause_report:
        impact = "В сообщении описано сохранение ставки; новое изменение ключевой ставки не объявлено. " + (
            "Влияние на процентный результат зависит от переоценки обеих сторон баланса; знак и размер эффекта не установлены."
            if context.get("bank") else
            "Проверьте сроки и условия переоценки договоров: пауза сама по себе не устанавливает изменение процентных расходов."
        )
    if read and role == "cmo" and topic in {"rates", "funding", "earnings"}:
        impact = "Если финансовый фактор меняет поведение целевых клиентов, стоит проверить спрос, позиционирование и предложения конкурентов. Изменение спроса именно в ваших сегментах не установлено."
        condition = "Нужно сопоставить событие с данными по клиентским сегментам и маркетинговыми приоритетами компании."
    if not read:
        role_action = "Прочитайте первоисточник и проверьте дату и содержание события."
    elif not factor:
        role_action = "Уточните связь с компанией: " + (missing[0].lower() if missing else "затронутые договоры, продукты или рынки") + "."
    elif role == "cmo":
        role_action = "Сопоставьте событие с вашими целевыми сегментами и планом маркетинга; проверьте применимость указанного фактора."
    elif mechanism == "bank_alm":
        role_action = {
            "cfo": "Сопоставьте гэп переоценки и стоимость привлечения с планом процентного результата.",
            "treasurer": "Сверьте сроки переоценки пассивов и активов с календарём фондирования и оттоков.",
            "ceo": "Запросите чувствительность процентного результата по структуре баланса перед изменением планов.",
        }.get(role, "Проверьте условия продуктов и сроки их переоценки по внутренним данным.")
    elif mechanism == "corporate_funding":
        role_action = {
            "cfo": "Сверьте тип ставки и даты переоценки договоров с бюджетом процентных расходов.",
            "treasurer": "Сопоставьте график погашений и переоценки с доступной ликвидностью и условиями рефинансирования.",
            "ceo": "Проверьте условия финансирования инвестиционных планов перед изменением сроков проектов.",
        }.get(role, "Проверьте, какие договоры и планы зависят от условий финансирования.")
    elif missing:
        role_action = "Проверьте «" + missing[0].lower() + "» применительно к фактору «" + factor["label"] + "»."
    if item.get("company_relation") == "query_only":
        factor = None
        impact = "Связь события с компанией не подтверждена; тематическое совпадение само по себе не устанавливает влияние."
        condition = "Сначала установите связь с договорами, продуктами или рынками компании."
        role_action = "Уточните, какие операции или контрагенты компании затронуты событием."
    elif read and conflicts:
        factor = None
        role_action = "Сверьте противоречащие значения: «" + conflicts[0] + "» в документах и подтверждённом профиле."
    future = bool(re.search(r"прогноз|ожида|планир|может|намерен|состоится|пройд[её]т", item["title"], re.I))
    severe = (
        read
        and item.get("company_relation") == "direct"
        and bool(
            re.search(
                r"дефолт|банкрот|отозван\w* лиценз|останов\w* работ|утечк\w* данн|кибератак|просроч",
                item["title"],
                re.I,
            )
        )
    )
    regulatory = (
        read
        and "capital" in topics
        and bool(re.search(r"норматив|регуля|закон|санкци|налог", item["title"], re.I))
    )
    return {
        "why": why,
        "mechanism": rule["focus"] if rule and read else "Требуется проверка источника",
        "impact": impact,
        "condition": condition,
        "action": role_action,
        "company_factor": factor,
        "company_factor_conflicts": conflicts,
        "missing_company_data": missing,
        "event_semantics": "rate_unchanged_report" if pause_report else "unclassified",
        "role": ROLE_NAMES.get(role, ROLE_NAMES["other"]),
        "kind": "source_scenario" if future else "source_message" if read else "search_lead",
        "signal": "Сигнал риска — проверьте связь"
        if severe
        else "Регуляторное изменение"
        if regulatory
        else "Возможное изменение"
        if future
        else "Событие для проверки",
        "urgent": severe,
        "materiality": "Размер влияния не установлен",
    }


def overview_brief(items, context, role):
    read = [item for item in items if item.get("read_status") == "article_read"]
    top = (read or items)[:3]
    if not top:
        return {
            "status": "no_sources",
            "title": "Главное для вашей компании",
            "situation": "Свежих прочитанных источников пока нет.",
            "next": "Соберите новости или добавьте источник в исследование.",
            "outlook": "Для обоснованного сценария нужны датированные источники и контекст компании.",
            "items": [],
        }
    first = top[0]
    view = first.get("decision") or news_interpretation(first, context, role)
    return {
        "status": "source_based_first_read",
        "title": "Главное для вашей компании",
        "situation": first["title"],
        "why": view["why"],
        "impact": view["impact"],
        "next": view["action"],
        "outlook": "Дальнейший сценарий зависит от условий: " + view["condition"],
        "items": [
            {
                "news_id": x["news_id"],
                "title": x["title"],
                "published_at": x["published_at"],
                "url": x["url"],
                "read_status": x.get("read_status"),
                "signal": (x.get("decision") or {}).get("signal"),
                "urgent": (x.get("decision") or {}).get("urgent", False),
            }
            for x in top
        ],
        "scope": "Первичная оценка по источникам и правилам. Условный канал влияния, не завершённый прогноз.",
    }

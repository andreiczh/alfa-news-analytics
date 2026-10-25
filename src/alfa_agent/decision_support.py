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
    why = ". ".join(item.get("reasons", [])[:2]) + "."
    if item.get("matched_priorities"):
        why += " Связано с вашим приоритетом: «" + item["matched_priorities"][0] + "»."
    role_action = ROLE_QUESTIONS.get(role, ROLE_QUESTIONS["other"])
    if not read:
        impact = "Текст источника пока не прочитан. Обоснованное влияние по одному заголовку не определено."
        condition = "Откройте первоисточник или запустите исследование для проверки сообщения."
    elif rule:
        impact, condition = rule["effect"], rule["condition"]
    else:
        impact = "По доступному тексту канал влияния на компанию не установлен."
        condition = "Нужно уточнить связь события с деятельностью и приоритетами компании."
    if read and role == "cmo" and topic in {"rates", "funding", "earnings"}:
        impact = "Если финансовый фактор меняет поведение целевых клиентов, стоит проверить спрос, позиционирование и предложения конкурентов. Изменение спроса именно в ваших сегментах не установлено."
        condition = "Нужно сопоставить событие с данными по клиентским сегментам и маркетинговыми приоритетами компании."
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

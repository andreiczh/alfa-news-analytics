"""Explainable editorial ranking, not a probability or a prediction of financial impact."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .identity import role_for
from .news_sources import publication_time

TOPIC_RULES = {
    "rates": (
        "Ставки и процентный результат",
        r"ключев\w* ставк|процентн\w* ставк|денежно.кредит|по ставке|ставк\w* по вклад",
    ),
    "funding": (
        "Фондирование и ликвидность",
        r"фондир|ликвидност|депозит|облигац|доходност|своп|репо|привлечен\w* средств|привлечён\w* средств",
    ),
    "capital": (
        "Капитал и регулирование",
        r"капитал|норматив|регулятор|регулирован|лицензи|штраф|санкци|комплаенс|инсайдер|манипулирован|резервирован",
    ),
    "earnings": (
        "Финансовые результаты",
        r"прибыл|убыт|финансов\w* результат|отчетност|отчётност|рентабельн|процентн\w* доход",
    ),
    "credit": (
        "Кредитный риск",
        r"дефолт|банкрот|просроч|кредитн\w* рейтинг|стоимост\w* риска|качеств\w* (?:кредит|портфел)|неплатеж|неплатёж|гаранти",
    ),
    "fx": ("Валюта и внешние расчёты", r"валют|курс\w* рубл|доллар|юан|трансгранич|экспорт|импорт"),
    "inflation": (
        "Инфляция и затраты",
        r"инфляц|потребительск\w* цен|себестоимост|подорожан|тариф|рост\w* затрат|\bналог\w*|\bндс\b|зарплат|оплат\w* труда",
    ),
    "operations": (
        "Операционные процессы",
        r"сбо[йя]|кибер|утечк|недоступ|остановк|отключ|автоматизац|цифров\w* рубл|платежн\w* систем|платёжн\w* систем",
    ),
    "demand": ("Спрос и продажи", r"спрос|продаж|потреблен|потреблен|розничн|выпуск\w* продукц"),
    "products": (
        "Продукты и клиенты",
        r"продукт|кешбэк|кэшбэк|тариф|гаранти|экспресс|запуска|запуст|видеоплатформ",
    ),
    "strategy": (
        "Стратегия и инвестиции",
        r"сделк|поглощ|слияни|инвестиц|стратег|дол[яию] в|партнер|партнёр",
    ),
    "brand": (
        "Бренд и коммуникации",
        r"преми[яию]|наград|лаунж|лаундж|бизнес.зал|образовательн\w* курс|победил|фестивал|саммит",
    ),
}
ROLE_WEIGHTS = {
    "cfo": {
        "rates": 25,
        "funding": 25,
        "capital": 23,
        "earnings": 27,
        "credit": 21,
        "fx": 21,
        "inflation": 19,
        "operations": 11,
        "strategy": 19,
        "products": 8,
        "brand": 1,
        "demand": 12,
    },
    "treasurer": {
        "rates": 28,
        "funding": 30,
        "capital": 18,
        "earnings": 9,
        "credit": 19,
        "fx": 27,
        "inflation": 13,
        "operations": 18,
        "strategy": 9,
        "products": 6,
        "brand": 0,
        "demand": 4,
    },
    "ceo": {
        "rates": 18,
        "funding": 18,
        "capital": 24,
        "earnings": 24,
        "credit": 22,
        "fx": 13,
        "inflation": 12,
        "operations": 22,
        "strategy": 30,
        "products": 18,
        "brand": 7,
        "demand": 20,
    },
    "coo": {
        "rates": 7,
        "funding": 11,
        "capital": 17,
        "earnings": 10,
        "credit": 9,
        "fx": 11,
        "inflation": 24,
        "operations": 32,
        "strategy": 17,
        "products": 21,
        "brand": 3,
        "demand": 13,
    },
    "cco": {
        "rates": 13,
        "funding": 8,
        "capital": 11,
        "earnings": 14,
        "credit": 11,
        "fx": 9,
        "inflation": 12,
        "operations": 14,
        "strategy": 20,
        "products": 32,
        "brand": 22,
        "demand": 28,
    },
    "other": {k: 15 for k in TOPIC_RULES if k != "brand"},
}
ROLE_LABELS = {
    "cfo": "CFO",
    "treasurer": "казначей",
    "ceo": "CEO",
    "coo": "COO",
    "cco": "CCO",
    "other": "руководитель",
}
PRIMARY_DOMAINS = {
    "cbr.ru",
    "moex.com",
    "acra-ratings.ru",
    "raexpert.ru",
    "rosstat.gov.ru",
    "nalog.gov.ru",
    "minfin.gov.ru",
}
MEDIA_DOMAINS = {
    "rbc.ru",
    "interfax.ru",
    "finmarket.ru",
    "kommersant.ru",
    "vedomosti.ru",
    "tass.ru",
    "banki.ru",
}


def domain_of(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def within_domain(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def normalize_name(value):
    value = re.sub(r"\b(пао|ао|ооо|оао|зао|акционерное|общество)\b", "", value.casefold())
    return "".join(re.findall(r"[a-zа-яё0-9]+", value))


def company_context(snapshot):
    features = snapshot.get("profile", {}).get("features", [])
    good = [
        f for f in features if f.get("status") not in {"missing", "conflicted", "conflict", "conflicting"}
    ]
    name = next((f.get("text_value") for f in good if f["name"] == "company_name"), "") or ""
    industry = next((f.get("text_value") for f in good if f["name"] == "industry"), "") or ""
    bank = bool(re.search(r"банк|\bbank\b", name + " " + industry, re.I))
    exposure_topics = set()
    # Feature names alone never imply exposure: require a present value.
    for f in good:
        if re.search(
            r"не предостав|неизвест|не указан|нет данн|unknown|not provided", f.get("text_value") or "", re.I
        ):
            continue
        present = (
            f.get("boolean_value") is True or bool(f.get("text_value")) or f.get("numeric_value") is not None
        )
        if not present or f.get("boolean_value") is False:
            continue
        if f["name"] in {
            "floating_debt_rub",
            "rate_type",
            "net_interest_income",
            "repricing_gap",
            "deposit_beta",
        }:
            exposure_topics |= {"rates", "funding"}
        if f["name"] in {"funding_structure", "liquidity_buffer"}:
            exposure_topics.add("funding")
        if f["name"] in {"foreign_currency_exposure", "currency_exposure", "export_share", "import_share"}:
            exposure_topics.add("fx")
    return {
        "name": name,
        "industry": industry,
        "bank": bank,
        "exposure_topics": sorted(exposure_topics),
        "website": snapshot.get("context", {}).get("website", ""),
        "snapshot_id": snapshot.get("snapshot_id"),
        "profile_version": snapshot.get("profile", {}).get("version"),
        "anonymous": bool(re.search(r"\b[UFG]_\d{3,}\b", name)),
    }


def topics_for(item):
    text = item["title"] + " " + item.get("excerpt", "")[:1500]
    # A proper company name ending in "Капитал" does not describe bank capital.
    text = re.sub(r"[А-ЯЁA-Z][а-яёa-z]+[- ]+Капитал\b", " ", text)
    if re.search(r"приглаша\w* студентов|чемпионат|научн\w* конференц", item["title"], re.I):
        return ["brand"]
    result = [key for key, (_, expression) in TOPIC_RULES.items() if re.search(expression, text, re.I)]
    if "fx" in result and re.search(r"(?:продаж|покупк)\w*.{0,35}(?:валют|юан|доллар)", item["title"], re.I):
        result = [key for key in result if key != "demand"]
    return result


def score_item(item, context, role, now, window_days=7):
    role = role if role in ROLE_WEIGHTS else role_for(role)
    stamp = publication_time(item.get("published_at"))
    if not stamp:
        return None, "unknown_date"
    date = datetime.fromisoformat(stamp)
    age = (now.astimezone(timezone.utc) - date).total_seconds() / 86400
    if age < -5 / 1440:
        return None, "future_date"
    if age > window_days:
        return None, "outside_window"
    if item.get("read_status") == "date_conflict":
        return None, "date_conflict"
    topics = topics_for(item)
    title = item["title"]
    name_key = normalize_name(context["name"])
    direct = not context["anonymous"] and len(name_key) >= 4 and name_key in normalize_name(title)
    analyst = direct and bool(
        re.search(
            r"сохраняет оценку|оставляет рейтинг|для акций|наш прогноз|ожида[ею]т от цб|жд[её]м от цб",
            title,
            re.I,
        )
    )
    own_domain = domain_of(context.get("website", ""))
    host = domain_of(item["url"])
    primary = within_domain(host, PRIMARY_DOMAINS) or bool(own_domain and within_domain(host, {own_domain}))
    macro = bool(set(topics) & {"rates", "inflation", "fx"})
    sector = context["bank"] and bool(set(topics) & {"rates", "funding", "capital", "credit", "operations"})
    industry_words = {w[:6] for w in re.findall(r"[а-яёa-z]{5,}", context["industry"].casefold())} - {
        "деятел",
        "прочие",
        "услуг",
    }
    industry_match = bool(industry_words & {w[:6] for w in re.findall(r"[а-яёa-z]{5,}", title.casefold())})
    if not direct and not sector and not macro and not industry_match:
        return None, "not_company_or_exposure"
    if not topics:
        return None, "no_business_channel"
    weights = ROLE_WEIGHTS[role]
    headline_topics = topics_for({"title": title, "excerpt": ""})
    main_topic = max(headline_topics or topics, key=lambda t: weights.get(t, 0))
    affinity = weights.get(main_topic, 0)
    if affinity == 0:
        return None, "low_role_priority"
    reasons, components = [], {}
    if direct:
        components["company"] = 24 if not analyst else 9
        reasons.append(
            "Компания названа в заголовке"
            if not analyst
            else "Банк выступает комментатором рынка; это не событие его собственного бизнеса"
        )
    elif sector:
        components["company"] = 22
        reasons.append("Событие банковского сектора; компания указана как банк")
    elif industry_match:
        components["company"] = 18
        reasons.append("Есть совпадение с основной деятельностью из профиля")
    else:
        components["company"] = 10
        reasons.append("Макроэкономический фактор; прямую экспозицию компании нужно проверить")
    components["role"] = affinity
    reasons.append("Для " + ROLE_LABELS[role] + ": " + TOPIC_RULES[main_topic][0].lower())
    components["freshness"] = 15 if age <= 1 else 12 if age <= 3 else 9 if age <= 7 else 5 if age <= 30 else 1
    components["source"] = 12 if primary else 8 if within_domain(host, MEDIA_DOMAINS) else 3
    components["read"] = 6 if item.get("read_status") == "article_read" else 0
    policy_decision = bool(
        re.search(r"(?:решени\w*|сохран\w*|повыс\w*|сниз\w*).{0,35}ключев\w* ставк", title, re.I)
    )
    components["event"] = 6 if policy_decision else -9 if item.get("feed_id") == "cbr_updates" else 0
    announcement = bool(re.search(r"\bсостоится\b|\bпройд[её]т\b|\bприглаша\w*|\bанонс\w*", title, re.I))
    components["announcement_penalty"] = -16 if announcement else 0
    if announcement:
        reasons.append("Это анонс, а не сообщение об итогах события; проверьте дату мероприятия")
    other_issuer_risk = not direct and bool(re.search(r"отозван\w* лицензи|дефолт|банкрот", title, re.I))
    components["unconfirmed_counterparty_penalty"] = -12 if other_issuer_risk else 0
    if other_issuer_risk:
        reasons.append("Событие другого участника рынка: экспозиция вашей компании на него не подтверждена")
    matched = sorted(set(topics) & set(context["exposure_topics"]))
    components["profile"] = 8 if matched else 0
    if matched:
        reasons.append("В профиле есть сведения по связанному фактору: " + TOPIC_RULES[matched[0]][0].lower())
    promo = "brand" in topics and not set(topics) & {"earnings", "capital", "credit", "funding"}
    components["promotion_penalty"] = (
        -24 if promo and role in {"cfo", "treasurer"} else -8 if promo and role in {"ceo", "coo"} else 0
    )
    if components["promotion_penalty"]:
        reasons.append("Имиджевый или рекламный материал имеет пониженный приоритет для этой роли")
    score = max(0, min(100, sum(components.values())))
    if score < 25:
        return None, "low_role_priority"
    priority = "check_now" if score >= 70 else "monitor" if score >= 48 else "background"
    critical = bool(set(topics) & {"credit", "operations", "capital"}) and direct and not analyst
    if critical and priority == "background":
        priority = "monitor"
    return {
        **item,
        "score": score,
        "score_components": components,
        "reasons": reasons,
        "topics": topics,
        "focus": TOPIC_RULES[main_topic][0],
        "priority": priority,
        "company_relation": "market_commentary"
        if analyst
        else "direct"
        if direct
        else "sector"
        if sector or industry_match
        else "macro",
        "source_level": "primary"
        if primary
        else "business_media"
        if within_domain(host, MEDIA_DOMAINS)
        else "other",
        "age_days": round(max(0, age), 2),
    }, None


def duplicate_story(a, b):
    if a["url"] == b["url"]:
        return True
    if not a.get("published_at") or not b.get("published_at"):
        return False
    if (
        abs(
            (
                datetime.fromisoformat(a["published_at"]) - datetime.fromisoformat(b["published_at"])
            ).total_seconds()
        )
        > 2 * 86400
    ):
        return False

    def tokens(title):
        return set(re.findall(r"[а-яёa-z]{3,}|\d+(?:[.,]\d+)?", title.casefold()))

    left, right = tokens(a["title"]), tokens(b["title"])

    def numbers(title):
        return tuple(re.findall(r"[-+−]?\d+(?:[.,]\d+)?", title))

    def direction(title):
        return tuple(
            bool(re.search(pattern, title, re.I))
            for pattern in (
                r"повыс|увелич|вырос|рост",
                r"сниз|сократ|упал|паден",
                r"сохран|без изменен",
                r"ожида|прогноз|планир|может|снизит|повысит|сохранит",
            )
        )

    return (
        bool(left and right)
        and numbers(a["title"]) == numbers(b["title"])
        and direction(a["title"]) == direction(b["title"])
        and len(left & right) / len(left | right) >= 0.85
    )


def rank_news(items, context, role, now, window_days=7):
    ranked, excluded = [], {}
    for item in items:
        scored, reason = score_item(item, context, role, now, window_days)
        if scored:
            ranked.append(scored)
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    ranked.sort(key=lambda x: (x["score"], x["published_at"], x["news_id"]), reverse=True)
    unique = []
    for item in ranked:
        previous = next((p for p in unique if duplicate_story(p, item)), None)
        if previous:
            previous.setdefault("related_sources", []).append(
                {k: item[k] for k in ("url", "publisher", "published_at", "title")}
            )
            excluded["duplicate_story"] = excluded.get("duplicate_story", 0) + 1
        else:
            unique.append(dict(item))
    return unique[:20], excluded

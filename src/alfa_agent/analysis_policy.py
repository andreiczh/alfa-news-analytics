"""Server-owned temporal and financial boundaries, independent of a model verdict.

These are conservative publication contracts, not a universal financial model.
Source quotations remain untouched. Planning dates are calculated, never forecasts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

POLICY_VERSION = "2026-10-25-v6"


RELATIVE_CALENDAR = re.compile(
    r"\b(?:вчера|сегодня|завтра|послезавтра|позавчера|накануне)\b|"
    r"\b(?:дн[её]м|за\s+(?:один|два|три|\d+)\s+дн\w*|через\s+(?:день|\d+\s+дн\w*))\s*(?:до|после)?\b",
    re.I,
)
TITLE_TIME = re.compile(
    r"\b(?:до\s+конца|после\s+(?:декабр|январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр)|"
    r"(?:на|до|после|в)\s+\w*\s*(?:заседани|квартал|полугоди)|20\d{2}|"
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|"
    r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b",
    re.I,
)
NET_METRIC = re.compile(
    r"марж\w*|прибыл\w*|чист\w*\s+процентн\w*\s+(?:доход\w*|результат\w*)|\bNIM\b|\bNII\b", re.I
)
DIRECTION = re.compile(
    r"выраст\w*|раст[её]т|растут|увелич\w*|сократ\w*|сниз\w*|снижа\w*|упад\w*|пада\w*|"
    r"сохран\w*|стабилиз\w*|стабил\w*|держ\w*|улучш\w*|ухудш\w*|под\s+давлени\w*",
    re.I,
)
CONDITIONAL = re.compile(
    r"\bесли\b|при\s+(?:условии|росте|снижении|опережающ|превышении|изменении)|зависит\s+от", re.I
)
NEW_COST = re.compile(
    r"(?:кредит|за[её]м|фондирован|долг)\w*[^.!?]{0,65}(?:дорожа\w*|подорож\w*|станов\w*\s+дороже|удорожа\w*)|"
    r"рост\w*\s+стоимост\w*\s+(?:кредит|за[её]м|фондирован|долг)",
    re.I,
)
SEPARATE_CAUSE = re.compile(r"лаг\w*|предыдущ\w*\s+(?:повыш|переоцен)|спред\w*|преми\w*\s+за\s+риск", re.I)


def planning_policy(as_of: str, days: int = 30):
    start = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Moscow")).date()
    if days not in {7, 30, 90, 365}:
        raise ValueError("invalid_planning_days")
    end = start + timedelta(days=days - 1)
    return {
        "version": "1.0",
        "as_of": start.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "horizon": f"Плановый период с {start.isoformat()} по {end.isoformat()} включительно",
        "basis": "user_selected_planning_window_not_a_forecast",
        "rules": [
            "horizon каждого сценария в точности равен horizon политики; календарь не помещать в title",
            "В собственном тексте использовать абсолютные даты с опорами; относительные дни запрещены",
            "Исторический прогноз автора остаётся историческим и не определяет текущий исход",
            "Не обещать исход за пределами планового периода; указывать условия и сигналы пересмотра",
        ],
    }


def sentence_issues(text: str, *, bank: bool, pause: bool, fixed_debt: bool = False):
    issues = []
    for sentence in re.split(r"(?<!\d)[.!?](?!\d)|[;\n]", text):
        # Negated inability to establish the sign is valid, not a directional forecast.
        # A disclaimer elsewhere must not excuse an opposite categorical promise.
        unknown = re.search(r"не\s+гарантир", sentence, re.I) and not re.search(
            r"\b(?:но|однако|хотя)\b", sentence, re.I
        )
        if bank and NET_METRIC.search(sentence) and DIRECTION.search(sentence) and not unknown:
            if not CONDITIONAL.search(sentence) or not re.search(
                r"может|могут|риск|потенциальн", sentence, re.I
            ):
                issues.append("bank_net_effect_requires_balance_model")
            elif not re.search(r"актив\w*|пассив\w*|обязательств\w*|бета|гэп|переоцен", sentence, re.I):
                issues.append("bank_net_effect_condition_needs_balance_channel")
        if pause and NEW_COST.search(sentence) and not unknown:
            if not (SEPARATE_CAUSE.search(sentence) and CONDITIONAL.search(sentence)):
                issues.append("pause_is_not_a_new_cost_shock")
        if fixed_debt and re.search(
            r"(?:сниз\w*|сократ\w*|выраст\w*|увелич\w*)[^.!?]{0,50}(?:процентн\w*\s+расход|плат[её]ж\w*\s+по\s+(?:долг|кредит))",
            sentence,
            re.I,
        ):
            if not unknown and not re.search(
                r"рефинанс|нов\w*\s+(?:долг|кредит)|договор\w*\s+(?:предусматр|допуска)|пересмотр\w*\s+услов",
                sentence,
                re.I,
            ):
                issues.append("fixed_debt_does_not_reprice_automatically")
    return list(dict.fromkeys(issues))


def policy_issues(value, ledger, *, as_of, days=30, bank=False, profile=None):
    from .narrative import narrative_points, calendar_dates

    policy = planning_policy(as_of, days)
    issues = []
    pause = any(
        a.get("kind") in {"public_page", "user_news"}
        and re.search(
            r"(?:сохран\w*|остав\w*)[^.!?]{0,80}(?:ключев\w*\s+ставк|ставк\w*\s+на\s+уровне)",
            a["quote"],
            re.I,
        )
        for a in ledger
    )
    features = {
        f["name"]: f
        for f in (profile or {}).get("features", [])
        if f.get("status") not in {"missing", "conflicting"}
    }
    fixed = features.get("rate_type", {}).get("text_value") == "fixed"
    for index, point in enumerate(narrative_points(value)):
        text = point.text + " " + point.condition
        if point.kind in {"source_fact", "source_forecast", "source_assessment"}:
            quotes = [a["quote"] for a in ledger if a["anchor_id"] in point.anchor_ids]
            normalized = " ".join(point.text.split())
            if len(point.anchor_ids) != 1 or not any(normalized in " ".join(q.split()) for q in quotes):
                issues.append(f"source_message_must_be_one_verbatim_quote:point[{index}]")
            if any(a.get("excerpt_complete") is False for a in ledger if a["anchor_id"] in point.anchor_ids):
                issues.append(f"source_message_requires_complete_excerpt:point[{index}]")
            if point.kind == "source_fact" and re.search(
                r"прогноз\w*|ожида\w*|предполага\w*|может\s+(?:сниз|выраст|повыс|сохран)|вероятност",
                point.text,
                re.I,
            ):
                issues.append(f"source_forecast_cannot_be_labeled_fact:point[{index}]")
            if point.kind == "source_fact" and any(
                a.get("claim_type") in {"ASSESSMENT", "FORECAST", "SCENARIO", "RECOMMENDATION"}
                for a in ledger
                if a["anchor_id"] in point.anchor_ids
            ):
                issues.append(f"source_opinion_cannot_be_labeled_fact:point[{index}]")
            issues.extend(
                f"{issue}:condition[{index}]"
                for issue in sentence_issues(point.condition, bank=bank, pause=pause, fixed_debt=fixed)
            )
        # A source quotation retains its original tense and relative words.
        # The publication date is displayed separately. Only our prose is asserted as of now.
        own_prose = (
            point.condition if point.kind in {"source_fact", "source_forecast", "source_assessment"} else text
        )
        if RELATIVE_CALENDAR.search(own_prose):
            issues.append(f"relative_calendar_not_allowed:point[{index}]")
        for sentence in re.split(r"(?<!\d)[.!?](?!\d)|[;\n]", own_prose):
            dates = [d for d in calendar_dates(sentence) if not d.startswith("--")]
            if (
                dates
                and all(d < policy["as_of"] for d in dates)
                and re.search(
                    r"предстоящ|состоится|пройд[её]т|будет\s+опубликован|следующ\w*\s+заседани",
                    sentence,
                    re.I,
                )
            ):
                issues.append(f"past_event_presented_as_future:point[{index}]")
        if point.kind not in {"source_fact", "source_forecast", "source_assessment"}:
            issues.extend(
                f"{issue}:point[{index}]"
                for issue in sentence_issues(text, bank=bank, pause=pause, fixed_debt=fixed)
            )
        if point.kind == "source_fact" and re.search(
            r"\b(?:следует|рекомендуем|стоит\s+(?:проверить|снизить|повысить)|необходимо\s+проверить)\b",
            point.text,
            re.I,
        ):
            issues.append(f"source_fact_contains_own_recommendation:point[{index}]")
    if any(p.kind not in {"source_fact", "source_forecast", "source_assessment"} for p in value.situation):
        issues.append("situation_requires_source_messages")
    if any(
        p.kind not in {"interpretation", "hypothesis"}
        for p in [
            *value.drivers,
            *value.company_impact,
            *value.risks,
            *value.opportunities,
            *value.recommendations,
        ]
    ):
        issues.append("own_reasoning_cannot_be_labeled_source_fact")
    for index, scenario in enumerate(value.scenarios):
        if scenario.horizon != policy["horizon"]:
            issues.append(f"scenario_horizon_must_match_policy:scenario[{index}]")
        if TITLE_TIME.search(scenario.title):
            issues.append(f"scenario_title_must_not_extend_horizon:scenario[{index}]")
        text = " ".join(
            [
                scenario.title,
                scenario.outcome,
                scenario.mechanism,
                *scenario.assumptions,
                *scenario.change_signals,
                scenario.confidence_reason,
            ]
        )
        if RELATIVE_CALENDAR.search(text):
            issues.append(f"relative_calendar_not_allowed:scenario[{index}]")
        # An outcome cannot quietly extend a bounded scenario to a different calendar.
        for d in calendar_dates(scenario.outcome):
            if not d.startswith("--") and d > policy["end"]:
                issues.append(f"outcome_outside_planning_window:scenario[{index}]")
        if re.search(r"до\s+конца\s+(?:года|квартала)|после\s+декабр", scenario.outcome, re.I):
            issues.append(f"outcome_uses_unbounded_calendar:scenario[{index}]")
        for signal in scenario.change_signals:
            if any(d < policy["as_of"] for d in calendar_dates(signal) if not d.startswith("--")):
                issues.append(f"past_date_in_future_signal:scenario[{index}]")
        issues.extend(
            f"{issue}:scenario[{index}]"
            for issue in sentence_issues(text, bank=bank, pause=pause, fixed_debt=fixed)
        )
    return list(dict.fromkeys(issues))

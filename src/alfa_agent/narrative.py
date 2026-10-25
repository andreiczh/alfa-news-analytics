"""Coherent decision memo with per-claim evidence and conditional scenario contracts."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NarrativePoint(Strict):
    kind: Literal["source_fact", "source_forecast", "source_assessment", "interpretation", "hypothesis"]
    text: str = Field(min_length=15, max_length=700)
    anchor_ids: list[str] = Field(min_length=1, max_length=3)
    condition: str = Field(min_length=5, max_length=400)


class MainConclusion(NarrativePoint):
    kind: Literal["interpretation", "hypothesis"]


class ForecastScenario(Strict):
    variant: Literal["base", "alternative"]
    title: str = Field(min_length=5, max_length=120)
    outcome: str = Field(min_length=20, max_length=650)
    horizon: str = Field(min_length=5, max_length=180)
    mechanism: str = Field(min_length=20, max_length=700)
    assumptions: list[str] = Field(min_length=1, max_length=3)
    change_signals: list[str] = Field(min_length=1, max_length=3)
    anchor_ids: list[str] = Field(min_length=1, max_length=3)
    confidence_reason: str = Field(min_length=20, max_length=450)


class DecisionNarrative(Strict):
    title: str = Field(min_length=10, max_length=150)
    main_conclusion: MainConclusion
    situation: list[NarrativePoint] = Field(min_length=1, max_length=3)
    drivers: list[NarrativePoint] = Field(min_length=1, max_length=3)
    company_impact: list[NarrativePoint] = Field(min_length=1, max_length=3)
    risks: list[NarrativePoint] = Field(default_factory=list, max_length=3)
    opportunities: list[NarrativePoint] = Field(default_factory=list, max_length=3)
    recommendations: list[NarrativePoint] = Field(min_length=1, max_length=3)
    forecast_status: Literal["conditional_scenarios", "insufficient_evidence"]
    forecast_limit: str = Field(min_length=15, max_length=1000)
    scenarios: list[ForecastScenario] = Field(default_factory=list, max_length=3)
    context_keys: list[str] = Field(default_factory=list, max_length=8)


class SourceMessage(Strict):
    message_id: str = Field(min_length=2, max_length=80)
    condition: str = Field(min_length=5, max_length=400)


class NarrativeSelection(DecisionNarrative):
    """The LLM selects IDs; it cannot write or relabel source messages."""

    situation: list[SourceMessage] = Field(min_length=1, max_length=3)


def source_message_options(ledger):
    result = []
    external = [a for a in ledger if a["kind"] in {"public_page", "user_news", "bank_claim", "bank_excerpt"}]
    for anchor in external or ledger:
        if not anchor.get("excerpt_complete", True):
            continue
        quote = anchor["quote"]
        boundaries = [0, *[m.end() for m in re.finditer(r"(?<!\d)[.!?](?!\d)(?=\s|$)", quote)], len(quote)]
        passages = [quote[start:end].strip() for start, end in zip(boundaries, boundaries[1:])]
        passages = [text for text in passages if 30 <= len(text) <= 700]
        if not passages and 15 <= len(quote) <= 700:
            passages = [quote]
        for index, text in enumerate(passages[:3]):
            forecast = anchor.get("claim_type") in {"FORECAST", "SCENARIO"} or bool(
                re.search(
                    r"прогноз\w*|ожида\w*|предполага\w*|может\s+(?:сниз|выраст|повыс|сохран)|вероятност",
                    text,
                    re.I,
                )
            )
            kind = (
                "source_forecast"
                if forecast
                else "source_assessment"
                if anchor.get("claim_type") in {"ASSESSMENT", "RECOMMENDATION"}
                else "source_fact"
            )
            result.append(
                {
                    "message_id": f"msg_{anchor['anchor_id']}_{index}",
                    "anchor_id": anchor["anchor_id"],
                    "text": text,
                    "kind": kind,
                    "published_at": anchor.get("published_at"),
                }
            )
    return result[:48]


def bind_source_messages(selection, options):
    choices = {x["message_id"]: x for x in options}
    situation = []
    for selected in selection.situation:
        if selected.message_id not in choices:
            raise ValueError("unknown_source_message")
        option = choices[selected.message_id]
        situation.append(
            {
                "kind": option["kind"],
                "text": option["text"],
                "anchor_ids": [option["anchor_id"]],
                "condition": selected.condition,
            }
        )
    return DecisionNarrative.model_validate({**selection.model_dump(mode="json"), "situation": situation})


def decision_schema(ledger, confirmed_context, policy=None):
    schema = NarrativeSelection.model_json_schema() if policy else DecisionNarrative.model_json_schema()
    allowed = [a["anchor_id"] for a in ledger]
    for name in ("NarrativePoint", "MainConclusion", "ForecastScenario"):
        schema["$defs"][name]["properties"]["anchor_ids"]["items"] = {"type": "string", "enum": allowed}
    available = [
        key
        for key in confirmed_context.get("fields", {})
        if any(a.get("context_key") == key or "_context_" + key + "_" in a["source_id"] for a in ledger)
    ]
    schema["properties"]["context_keys"]["items"] = (
        {"type": "string", "enum": available} if available else {"type": "string"}
    )
    schema["properties"]["context_keys"]["minItems"] = 1 if available else 0
    schema["properties"]["context_keys"]["maxItems"] = min(8, len(available))
    if policy:
        schema["$defs"]["ForecastScenario"]["properties"]["horizon"]["enum"] = [policy["horizon"]]
        schema["$defs"]["SourceMessage"]["properties"]["message_id"]["enum"] = [
            o["message_id"] for o in source_message_options(ledger)
        ]
        # These sections contain our reasoning. Enforce this in the provider schema
        # as well as in the server policy; a prompt alone is not a type boundary.
        schema["$defs"]["NarrativePoint"]["properties"]["kind"]["enum"] = ["interpretation", "hypothesis"]
    return schema


def probability_claim(text):
    for sentence in re.split(r"[!?\n;]|(?<!\d)\.(?!\d)", text):
        for marker in re.finditer(r"вероятност\w*", sentence, re.I):
            tail = re.split(r"(?<!\d),(?!\d)", sentence[marker.end() : marker.end() + 100])[0]
            if re.search(r"не (?:определ|оцен|рассчит|извест)|неизвест|не оценива", tail, re.I):
                continue
            for number in re.finditer(r"\d+(?:[.,]\d+)?", tail):
                suffix = tail[number.end() :]
                value = Decimal(number.group().replace(",", "."))
                if re.match(r"\s*(?:%|процент|/|из\s+\d)", suffix, re.I):
                    return True
                if value <= 1 and not re.match(r"\s*(?:год|лет|месяц|дн|руб|доллар)", suffix, re.I):
                    return True
    return False


def revision_details(value, ledger):
    from .research import numbers as raw_numbers

    def numbers(text):
        return raw_numbers(numeric_text(text))

    anchors = {a["anchor_id"]: a for a in ledger}
    named = [("main_conclusion", value.main_conclusion)]
    for field in ("situation", "drivers", "company_impact", "risks", "opportunities", "recommendations"):
        named.extend(
            (field + "[" + str(index) + "]", point) for index, point in enumerate(getattr(value, field))
        )
    result = []
    for index, (path, point) in enumerate(named):
        quotes = " ".join(
            anchors.get(aid, {}).get("quote", "") + " " + (anchors.get(aid, {}).get("published_at") or "")
            for aid in point.anchor_ids
        )
        result.append(
            {
                "point_index": index,
                "field": path,
                "allowed_numbers": sorted(numbers(quotes)),
                "allowed_measures": sorted(measures(quotes)),
                "allowed_calendar_dates": sorted(calendar_dates(quotes)),
                "unsupported_numbers": sorted(numbers(point.text + " " + point.condition) - numbers(quotes)),
                "unsupported_measures": sorted(
                    measures(point.text + " " + point.condition) - measures(quotes)
                ),
                "unsupported_calendar_dates": [
                    d
                    for d in sorted(calendar_dates(point.text + " " + point.condition))
                    if not supported_dates(d, quotes)
                ],
                "required_kind": (
                    "source_fact/source_forecast/source_assessment"
                    if path.startswith("situation")
                    else "interpretation/hypothesis"
                ),
                "exact_source_quotes": (
                    [
                        {
                            "anchor_id": aid,
                            "text": anchors[aid]["quote"],
                            "claim_type": anchors[aid].get("claim_type"),
                        }
                        for aid in point.anchor_ids
                        if aid in anchors
                    ]
                    if path.startswith("situation")
                    else []
                ),
            }
        )
    for index, scenario in enumerate(value.scenarios):
        quotes = " ".join(
            anchors.get(aid, {}).get("quote", "") + " " + (anchors.get(aid, {}).get("published_at") or "")
            for aid in scenario.anchor_ids
        )
        fields = {
            "title": scenario.title,
            "outcome": scenario.outcome,
            "mechanism": scenario.mechanism,
            "confidence_reason": scenario.confidence_reason,
            **{f"assumptions[{i}]": s for i, s in enumerate(scenario.assumptions)},
            **{f"change_signals[{i}]": s for i, s in enumerate(scenario.change_signals)},
        }
        result.append(
            {
                "field": "scenarios[" + str(index) + "]",
                "allowed_numbers": sorted(numbers(quotes)),
                "allowed_measures": sorted(measures(quotes)),
                "allowed_calendar_dates": sorted(calendar_dates(quotes)),
                "calendar_field_errors": {
                    path: {
                        "text": text,
                        "unsupported_dates": [
                            d for d in sorted(calendar_dates(text)) if not supported_dates(d, quotes)
                        ],
                    }
                    for path, text in fields.items()
                    if not supported_dates(text, quotes)
                },
            }
        )
    used = {aid for point in narrative_points(value) for aid in point.anchor_ids}
    used.update(aid for scenario in value.scenarios for aid in scenario.anchor_ids)
    quotes = " ".join(
        a["quote"] + " " + (a.get("published_at") or "") for aid, a in anchors.items() if aid in used
    )
    result.append(
        {
            "field": "title, forecast_limit",
            "allowed_numbers": sorted(numbers(quotes)),
            "allowed_calendar_dates": sorted(calendar_dates(quotes)),
        }
    )
    result.append(
        {
            "field": "context_keys",
            "available_context_anchors": {
                key: [a["anchor_id"] for a in ledger if a.get("context_key") == key]
                for key in sorted({a.get("context_key") for a in ledger if a.get("context_key")})
            },
            "rule": "Каждый заявленный ключ должен иметь свою опору в anchor_ids пунктов или сценариев. Неиспользованный ключ не перечислять.",
        }
    )
    return result


def narrative_points(value):
    return [
        value.main_conclusion,
        *value.situation,
        *value.drivers,
        *value.company_impact,
        *value.risks,
        *value.opportunities,
        *value.recommendations,
    ]


_RU_CARDINALS = [
    ("девятисот|девятьсот", 900),
    ("восьмисот|восемьсот", 800),
    ("семисот|семьсот", 700),
    ("шестисот|шестьсот", 600),
    ("пятисот|пятьсот", 500),
    ("четырехсот|четырёхсот|четыреста", 400),
    ("трехсот|трёхсот|триста", 300),
    ("двухсот|двести", 200),
    ("сто|ста", 100),
    (r"девяност\w*", 90),
    (r"восьмидесят\w*|восемьдесят", 80),
    (r"семидесят\w*|семьдесят", 70),
    (r"шестидесят\w*|шестьдесят", 60),
    (r"пятидесят\w*|пятьдесят", 50),
    (r"сорок\w*", 40),
    (r"тридцат\w*", 30),
    (r"двадцат\w*", 20),
    (r"девятнадцат\w*", 19),
    (r"восемнадцат\w*", 18),
    (r"семнадцат\w*", 17),
    (r"шестнадцат\w*", 16),
    (r"пятнадцат\w*", 15),
    (r"четырнадцат\w*", 14),
    (r"тринадцат\w*", 13),
    (r"двенадцат\w*", 12),
    (r"одиннадцат\w*", 11),
    ("десять|десяти", 10),
    ("девять|девяти", 9),
    ("восемь|восьми", 8),
    ("семь|семи", 7),
    ("шесть|шести", 6),
    ("пять|пяти", 5),
    ("четыре|четырех|четырёх", 4),
    ("три|трех|трёх", 3),
    ("два|две|двух", 2),
    ("один|одного|одной|одну|одним|одном", 1),
    ("ноль|нуля", 0),
]


_MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()


def calendar_parts(text):
    """Keep a calendar date distinct from a decimal or independently matching digits."""
    found = set()

    def replace(match, order):
        parts = match.groupdict()
        day, month = int(parts[order[0]]), int(parts[order[1]])
        year = int(parts["year"]) if parts.get("year") else None
        try:
            date(year or 2000, month, day)
        except ValueError:
            return match[0]
        found.add(f"{year:04d}-{month:02d}-{day:02d}" if year else f"--{month:02d}-{day:02d}")
        return (parts.get("cue") or "") + f"{day} {month}" + (f" {year}" if year else "")

    text = re.sub(
        r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?=T\d{2}:|\b)",
        lambda m: replace(m, ("day", "month")),
        text,
    )
    text = re.sub(
        r"\b(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./](?P<year>\d{4})\b",
        lambda m: replace(m, ("day", "month")),
        text,
    )
    text = re.sub(
        r"\b(?P<cue>(?:решени\w*|заседани\w*|дат[ауы])\s+)(?P<day>\d{1,2})\.(?P<month>\d{1,2})(?![\d.,]|\s*(?:%|процент|млн|млрд|руб))",
        lambda m: replace(m, ("day", "month")),
        text,
        flags=re.I,
    )
    for month, name in enumerate(_MONTHS, 1):

        def written(match, month=month):
            day = int(match["day"])
            year = int(match["year"]) if match["year"] else None
            try:
                date(year or 2000, month, day)
            except ValueError:
                return match[0]
            found.add(f"{year:04d}-{month:02d}-{day:02d}" if year else f"--{month:02d}-{day:02d}")
            return f"{day} {month}" + (f" {year}" if year else "")

        text = re.sub(
            r"\b(?P<day>\d{1,2})\s+" + name + r"\b(?:\s+(?P<year>\d{4})\b)?", written, text, flags=re.I
        )
    return text, found


def calendar_dates(text):
    return calendar_parts(text)[1]


def supported_dates(text, evidence):
    available = calendar_dates(evidence)
    return all(
        value in available or (value.startswith("--") and any(a.endswith(value[2:]) for a in available))
        for value in calendar_dates(text)
    )


def factual_text(text, as_of=None, planning_days=30):
    """Exclude only exact server-owned scope metadata from source fact checks.

    This never licenses an event on today's date. The date can be omitted only
    inside an explicit statement about unverified currency of the evidence.
    """
    if not as_of:
        return text
    from .analysis_policy import planning_policy

    policy = planning_policy(as_of, planning_days)
    text = re.sub(re.escape(policy["horizon"]), "плановый период", text, flags=re.I)
    return re.sub(
        r"актуальность\s+на\s+" + re.escape(policy["as_of"]) + r"\s+не\s+(?:проверена|подтверждена)",
        "актуальность на дату анализа не подтверждена",
        text,
        flags=re.I,
    )


def numeric_text(text):
    """Measured Russian cardinals also need numeric evidence; spelling out a value is no escape."""
    units = (
        r"(?:процент|руб|доллар|евро|юан|миллион|миллиард|тысяч|млн|млрд|тыс|месяц|дней|дня|день|лет|года)"
    )
    pattern = r"\b((?:[а-яё]+\s+){0,3}[а-яё]+)(\s+)(?=" + units + ")"

    def convert(match):
        words = match[1].split()
        values = []
        while words:
            value = next(
                (n for expression, n in _RU_CARDINALS if re.fullmatch(expression, words[-1], re.I)), None
            )
            if value is None:
                break
            words.pop()
            values.append(value)
        if not values:
            return match[0]
        return (" ".join(words) + " " if words else "") + str(sum(values)) + match[2]

    return re.sub(pattern, convert, calendar_parts(text)[0], flags=re.I)


def measures(text):
    """Bind numerical assertions to their scale and dimension, not only a digit set."""
    pattern = r"(?<![\w.,])([-+−]?\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)\s*(млрд|миллиард\w*|млн|миллион\w*|тыс\w*)?\s*(процентн\w*\s+пункт\w*|п\.\s*п\.|б\.\s*п\.|%|процент\w*|руб\w*|RUB|₽|USD|доллар\w*|EUR|евро|CNY|юан\w*|дн\w*|день|дней|месяц\w*|год\w*|лет)(?![\w])"
    result = set()
    for match in re.finditer(pattern, numeric_text(text), re.I):
        raw, scale, unit = match.groups()
        number = Decimal(re.sub(r"[ \u00a0\u202f]", "", raw).replace(",", ".").replace("−", "-"))
        scale, unit = (scale or "").casefold(), unit.casefold()
        factor = (
            1_000_000_000
            if scale.startswith(("млрд", "миллиард"))
            else 1_000_000
            if scale.startswith(("млн", "миллион"))
            else 1000
            if scale.startswith("тыс")
            else 1
        )
        if unit.startswith(("руб", "rub", "₽")):
            dimension = "RUB"
        elif unit.startswith(("usd", "доллар")):
            dimension = "USD"
        elif unit.startswith(("eur", "евро")):
            dimension = "EUR"
        elif unit.startswith(("cny", "юан")):
            dimension = "CNY"
        elif unit.startswith("п.") or "пункт" in unit:
            dimension = "percentage_points"
        elif unit.startswith("б."):
            dimension = "basis_points"
        elif unit.startswith(("процент", "%")):
            dimension = "percent"
        elif unit.startswith(("дн", "день")):
            dimension = "days"
        elif unit.startswith("месяц"):
            dimension = "months"
        else:
            if 1900 <= number <= 2200:
                continue
            dimension = "years"
        result.add((str((number * factor).normalize()), dimension))
    return result


def validate_narrative(
    value,
    ledger,
    confirmed_context,
    bank_company=False,
    selected_news_id=None,
    *,
    as_of=None,
    planning_days=30,
    profile=None,
):
    from .research import numbers as raw_numbers

    def numbers(text):
        return raw_numbers(numeric_text(text))

    anchors = {a["anchor_id"]: a for a in ledger}
    pause_event = any(
        a["source_id"] == selected_news_id
        and re.search(r"сохран\w*[^.!?]{0,70}ключев\w*\s+ставк", a["quote"], re.I)
        for a in ledger
    )
    issues, all_evidence = [], []
    if len(set(value.context_keys)) != len(value.context_keys):
        issues.append("duplicate_narrative_context_key")
    fields = confirmed_context.get("fields", {})
    if any(key not in fields for key in value.context_keys):
        issues.append("unknown_narrative_context_key")
    if fields and not value.context_keys:
        issues.append("confirmed_context_not_used")
    used_ids = {aid for point in narrative_points(value) for aid in point.anchor_ids}
    used_ids.update(aid for scenario in value.scenarios for aid in scenario.anchor_ids)
    ungrounded_context_keys = [
        key
        for key in value.context_keys
        if not any(
            anchors.get(aid, {}).get("context_key") == key
            or "_context_" + key + "_" in anchors.get(aid, {}).get("source_id", "")
            for aid in used_ids
        )
    ]
    if ungrounded_context_keys:
        issues.append("confirmed_context_has_no_grounding")
        issues.extend("ungrounded_context_key:" + key for key in ungrounded_context_keys)
    for index, point in enumerate(narrative_points(value)):
        if any(aid not in anchors for aid in point.anchor_ids):
            issues.append("unknown_narrative_anchor")
            continue
        local = " ".join(
            anchors[aid]["quote"] + " " + (anchors[aid].get("published_at") or "") for aid in point.anchor_ids
        )
        all_evidence.append(local)
        checked_text = point.text + " " + factual_text(point.condition, as_of, planning_days)
        if not numbers(checked_text) <= numbers(local):
            issues.append("unsupported_narrative_number:" + str(index))
        if not supported_dates(checked_text, local):
            issues.append("unsupported_narrative_date:" + str(index))
        if not measures(point.text + " " + point.condition) <= measures(local):
            issues.append("unsupported_narrative_unit:" + str(index))
        if point.kind not in {"source_fact", "source_forecast", "source_assessment"} and probability_claim(
            point.text + " " + point.condition
        ):
            issues.append("unjustified_narrative_probability")
        if point.kind != "source_fact" and not point.condition.strip():
            issues.append("narrative_interpretation_needs_condition")
        if point.kind == "source_fact" and re.search(
            r"гарантир|обязательно|точно произойд", point.text, re.I
        ):
            issues.append("narrative_unwarranted_certainty")
        if (
            pause_event
            and point.kind in {"interpretation", "hypothesis"}
            and re.search(r"ставк|пауз", point.text, re.I)
            and re.search(
                r"(?:кредит|за[её]м|фондирован)\w*[^.!?]{0,70}(?:дорожа\w*|подорож\w*|станов\w*\s+дороже)",
                point.text,
                re.I,
            )
            and not re.search(
                r"может|возмож|если|лаг|ранее|предыдущ|спред|преми\w*\s+за\s+риск",
                point.text + " " + point.condition,
                re.I,
            )
        ):
            issues.append("pause_does_not_imply_new_cost_increase:" + str(index))
    for scenario in value.scenarios:
        all_evidence.extend(
            anchors[aid]["quote"] + " " + (anchors[aid].get("published_at") or "")
            for aid in scenario.anchor_ids
            if aid in anchors
        )
    if not numbers(value.title) <= numbers(" ".join(all_evidence)):
        issues.append("unsupported_narrative_title_number")
    if not numbers(value.forecast_limit) <= numbers(" ".join(all_evidence)):
        issues.append("unsupported_forecast_limit_number")
    if not supported_dates(value.title + " " + value.forecast_limit, " ".join(all_evidence)):
        issues.append("unsupported_narrative_overview_date")
    if value.forecast_status == "insufficient_evidence" and value.scenarios:
        issues.append("insufficient_forecast_cannot_publish_scenarios")
    if value.forecast_status == "conditional_scenarios":
        if {s.variant for s in value.scenarios} != {"base", "alternative"}:
            issues.append("forecast_needs_base_and_alternative")
        for scenario in value.scenarios:
            if bank_company and re.search(
                r"(?:марж\w*|прибыл\w*|процентн\w*\s+(?:доход\w*|результат\w*))\s+(?:банк\w*\s+)?(?:держ\w*|раст\w*|выраст\w*|вырос\w*|сохран\w*|стабил\w*|упад\w*|пада\w*|сниж\w*|сниз\w*|увелич\w*|сократ\w*)",
                scenario.title + ". " + scenario.outcome,
                re.I,
            ):
                issues.append("bank_scenario_result_requires_financial_model")
            if any(aid not in anchors for aid in scenario.anchor_ids):
                issues.append("unknown_forecast_anchor")
                continue
            local = " ".join(
                anchors[aid]["quote"] + " " + (anchors[aid].get("published_at") or "")
                for aid in scenario.anchor_ids
            )
            text = " ".join(
                [
                    scenario.title,
                    scenario.outcome,
                    *([] if as_of else [scenario.horizon]),
                    scenario.mechanism,
                    factual_text(scenario.confidence_reason, as_of, planning_days),
                    *scenario.assumptions,
                    *scenario.change_signals,
                ]
            )
            if not numbers(text) <= numbers(local):
                issues.append("unsupported_forecast_number")
            if not supported_dates(text, local):
                issues.append("unsupported_forecast_date")
            if not measures(text) <= measures(local):
                issues.append("unsupported_forecast_unit")
            if probability_claim(text) or re.search(r"гарантир|точно произойд", text, re.I):
                issues.append("unjustified_forecast_probability")
            if not any(
                anchors[aid]["kind"] in {"public_page", "bank_claim", "bank_excerpt", "user_news"}
                for aid in scenario.anchor_ids
            ):
                issues.append("forecast_needs_external_or_event_evidence")
    if bank_company and re.search(
        r"экономи\w* (?:на |по )?процент\w* расход\w*", value.main_conclusion.text, re.I
    ):
        issues.append("bank_narrative_corporate_debt_substitution")
    if as_of:
        from .analysis_policy import policy_issues

        issues.extend(
            policy_issues(value, ledger, as_of=as_of, days=planning_days, bank=bank_company, profile=profile)
        )
    return list(dict.fromkeys(issues))


def bind_narrative(value, ledger, role, context_revision):
    used = {aid for point in narrative_points(value) for aid in point.anchor_ids}
    used.update(aid for scenario in value.scenarios for aid in scenario.anchor_ids)
    result = {
        **value.model_dump(mode="json"),
        "role": role,
        "context_revision": context_revision,
        "evidence": [a for a in ledger if a["anchor_id"] in used],
        "scope": "Автоматически проверенные основания и условные сценарии. Не независимая экспертиза источников.",
    }
    anchors = {a["anchor_id"]: a for a in ledger}
    for point in result["situation"]:
        if point["anchor_ids"]:
            point["source_published_at"] = anchors[point["anchor_ids"][0]].get("published_at")
    return result

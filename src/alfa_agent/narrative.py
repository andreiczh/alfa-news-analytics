"""Coherent decision memo with per-claim evidence and conditional scenario contracts."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NarrativePoint(Strict):
    kind: Literal["source_fact", "source_forecast", "interpretation", "hypothesis"]
    text: str = Field(min_length=15, max_length=700)
    anchor_ids: list[str] = Field(min_length=1, max_length=3)
    condition: str = Field(min_length=5, max_length=400)


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
    main_conclusion: NarrativePoint
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


def decision_schema(ledger, confirmed_context):
    schema = DecisionNarrative.model_json_schema()
    allowed = [a["anchor_id"] for a in ledger]
    for name in ("NarrativePoint", "ForecastScenario"):
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

    return re.sub(pattern, convert, text, flags=re.I)


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


def validate_narrative(value, ledger, confirmed_context, bank_company=False):
    from .research import numbers as raw_numbers

    def numbers(text):
        return raw_numbers(numeric_text(text))

    anchors = {a["anchor_id"]: a for a in ledger}
    issues, all_evidence = [], []
    if len(set(value.context_keys)) != len(value.context_keys):
        issues.append("duplicate_narrative_context_key")
    fields = confirmed_context.get("fields", {})
    if any(key not in fields for key in value.context_keys):
        issues.append("unknown_narrative_context_key")
    if fields and not value.context_keys:
        issues.append("confirmed_context_not_used")
    used_ids = {aid for point in narrative_points(value) for aid in point.anchor_ids}
    if value.context_keys and not any(
        "_context_" + key + "_" in anchors.get(aid, {}).get("source_id", "")
        for key in value.context_keys
        for aid in used_ids
    ):
        issues.append("confirmed_context_has_no_grounding")
    for index, point in enumerate(narrative_points(value)):
        if any(aid not in anchors for aid in point.anchor_ids):
            issues.append("unknown_narrative_anchor")
            continue
        local = " ".join(
            anchors[aid]["quote"] + " " + (anchors[aid].get("published_at") or "") for aid in point.anchor_ids
        )
        all_evidence.append(local)
        if not numbers(point.text + " " + point.condition) <= numbers(local):
            issues.append("unsupported_narrative_number:" + str(index))
        if not measures(point.text + " " + point.condition) <= measures(local):
            issues.append("unsupported_narrative_unit:" + str(index))
        if point.kind not in {"source_fact", "source_forecast"} and probability_claim(
            point.text + " " + point.condition
        ):
            issues.append("unjustified_narrative_probability")
        if point.kind != "source_fact" and not point.condition.strip():
            issues.append("narrative_interpretation_needs_condition")
        if point.kind == "source_fact" and re.search(
            r"гарантир|обязательно|точно произойд", point.text, re.I
        ):
            issues.append("narrative_unwarranted_certainty")
    if not numbers(value.title) <= numbers(" ".join(all_evidence)):
        issues.append("unsupported_narrative_title_number")
    if not numbers(value.forecast_limit) <= numbers(" ".join(all_evidence)):
        issues.append("unsupported_forecast_limit_number")
    if value.forecast_status == "insufficient_evidence" and value.scenarios:
        issues.append("insufficient_forecast_cannot_publish_scenarios")
    if value.forecast_status == "conditional_scenarios":
        if {s.variant for s in value.scenarios} != {"base", "alternative"}:
            issues.append("forecast_needs_base_and_alternative")
        for scenario in value.scenarios:
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
                    scenario.horizon,
                    scenario.mechanism,
                    scenario.confidence_reason,
                    *scenario.assumptions,
                    *scenario.change_signals,
                ]
            )
            if not numbers(text) <= numbers(local):
                issues.append("unsupported_forecast_number")
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
    return list(dict.fromkeys(issues))


def bind_narrative(value, ledger, role, context_revision):
    used = {aid for point in narrative_points(value) for aid in point.anchor_ids}
    used.update(aid for scenario in value.scenarios for aid in scenario.anchor_ids)
    return {
        **value.model_dump(mode="json"),
        "role": role,
        "context_revision": context_revision,
        "evidence": [a for a in ledger if a["anchor_id"] in used],
        "scope": "Автоматически проверенные основания и условные сценарии. Не независимая экспертиза источников.",
    }

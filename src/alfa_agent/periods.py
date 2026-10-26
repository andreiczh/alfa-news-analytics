"""Conservative Russian period hints. Lookback and planning are separate axes.

Hints never silently override explicit controls. Unsupported or ambiguous periods
require the caller to show and confirm the actual supported settings.
"""

import re

NEWS_DAYS = (7, 30, 90)
PLANNING_DAYS = (7, 30, 90, 365)
NUMBER_WORDS = {"семь": 7, "тридцать": 30, "девяносто": 90, "три": 3, "один": 1}


def _days(number, unit):
    value = int(number) if number.isdigit() else NUMBER_WORDS.get(number)
    if value is None:
        return None
    multiplier = (
        7
        if unit.startswith("недел")
        else 30
        if unit.startswith("месяц")
        else 365
        if unit.startswith(("год", "лет"))
        else 1
    )
    return value * multiplier


def period_intent(question, news_days=7, planning_days=30):
    text = question.casefold().replace("ё", "е")
    hints = {"news": set(), "planning": set()}
    calendar_year = False
    pattern = r"(?<!\w)(за|последни[еийх]|на|ближайши[еийх])\s+(?:последни[еийх]\s+|ближайши[еийх]\s+)?(\d{1,4}|семь|тридцать|девяносто|три|один)\s*(дн\w*|день|недел\w*|месяц\w*|год\w*|лет)(?!\w)"
    for match in re.finditer(pattern, text):
        if match[2].isdigit() and int(match[2]) >= 1900 and match[3].startswith("год"):
            calendar_year = True
            continue
        axis = "news" if match[1] == "за" or match[1].startswith("последн") else "planning"
        days = _days(match[2], match[3])
        if days is not None:
            hints[axis].add(days)
    for prefix, axis in [(r"за|последнюю|последний", "news"), (r"на|ближайшую|ближайший", "planning")]:
        for word, days in [("неделю", 7), ("месяц", 30), ("квартал", 90), ("год", 365)]:
            if re.search(
                r"(?<!\w)(?:"
                + prefix
                + r")\s+(?:(?:последнюю|последний|ближайшую|ближайший)\s+)?"
                + word
                + r"(?!\w)",
                text,
            ):
                hints[axis].add(days)
    unresolved = calendar_year or bool(
        re.search(r"до конца (?:года|месяца|квартала)|с начала (?:года|месяца|квартала)", text)
    )
    detected = {axis: next(iter(values)) if len(values) == 1 else None for axis, values in hints.items()}
    supported = {"news": NEWS_DAYS, "planning": PLANNING_DAYS}
    selected = {"news": news_days, "planning": planning_days}
    issues = []
    for axis, values in hints.items():
        if len(values) > 1:
            issues.append({"axis": axis, "reason": "ambiguous", "values": sorted(values)})
        elif values:
            value = detected[axis]
            if value not in supported[axis]:
                issues.append({"axis": axis, "reason": "unsupported", "days": value})
            elif value != selected[axis]:
                issues.append({"axis": axis, "reason": "mismatch", "days": value})
    if unresolved:
        issues.append({"axis": "calendar", "reason": "calendar_boundary"})
    return {
        "news_days": news_days,
        "planning_days": planning_days,
        "detected_news_days": detected["news"],
        "detected_planning_days": detected["planning"],
        "issues": issues,
        "needs_confirmation": bool(issues),
        "scope": "Период относится к датам доступной подборки новостей. Документы компании и материалы банка сохраняют собственные даты; полнота за период не гарантирована.",
    }

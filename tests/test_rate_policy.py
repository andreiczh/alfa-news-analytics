import pytest
from alfa_agent.rate_policy import rate_pause_memo
from alfa_agent.narrative import narrative_points, validate_narrative
from alfa_agent.analysis_policy import sentence_issues

AS_OF = "2026-10-25T09:00:00+00:00"
SOURCE = {
    "source_id": "news_rate",
    "kind": "public_page",
    "title": "Банк России принял решение сохранить ключевую ставку",
    "published_at": "2026-10-23T10:30:00+00:00",
    "url": "https://www.cbr.ru/press/keypr/",
    "text": "Банк России принял решение сохранить ключевую ставку на уровне 14,00% годовых.",
}
LEDGER = [
    {
        "anchor_id": "a1",
        "source_id": "news_rate",
        "kind": "public_page",
        "published_at": SOURCE["published_at"],
        "quote": SOURCE["text"],
    },
    {
        "anchor_id": "a2",
        "source_id": "bank_rate",
        "kind": "bank_claim",
        "claim_type": "FORECAST",
        "published_at": "2026-09-11",
        "quote": "Мы ожидаем сохранения паузы по ключевой ставке.",
    },
    *[
        {
            "anchor_id": "a" + str(i),
            "source_id": "company_context_" + key + "_0",
            "kind": "company_evidence",
            "context_key": key,
            "published_at": None,
            "quote": text,
        }
        for i, key, text in [
            (3, "priorities", "Приоритеты компании — стоимость фондирования и условия договоров."),
            (4, "goals", "Цель компании — проверить спрос и устойчивость коммерческого плана."),
            (5, "industry", "Компания сообщила свою отрасль для настройки аналитики."),
            (6, "tracked_competitors", "Компания сообщила участников рынка для наблюдения."),
        ]
    ],
]
SNAPSHOT = {
    "context": {"fields": {a["context_key"]: {"value": a["quote"]} for a in LEDGER if a.get("context_key")}}
}


def build(bank=True, role="cfo", days=30, sources=None):
    request = {
        "question": "Как ставка влияет на фондирование и денежный план?",
        "as_of": AS_OF,
        "news_item_id": "news_rate",
        "role": role,
        "bank_company": bank,
        "planning_days": days,
    }
    value = rate_pause_memo(LEDGER, sources or [SOURCE], request, SNAPSHOT)
    if value:
        used = {aid for p in narrative_points(value) for aid in p.anchor_ids}
        value.context_keys = sorted(
            a["context_key"] for a in LEDGER if a.get("context_key") and a["anchor_id"] in used
        )
    return value


@pytest.mark.parametrize("bank", [True, False])
@pytest.mark.parametrize("role", ["cfo", "treasurer", "ceo", "coo", "cco", "cmo"])
@pytest.mark.parametrize("days", [7, 30, 90, 365])
def test_rate_core_is_consistent_for_roles_company_types_and_windows(bank, role, days):
    value = build(bank, role, days)
    assert value is not None
    assert not validate_narrative(value, LEDGER, SNAPSHOT["context"], bank, as_of=AS_OF, planning_days=days)
    assert "на весь плановый период" not in value.main_conclusion.text
    assert len({s.horizon for s in value.scenarios}) == 1
    assert all(s.outcome.startswith("Если") for s in value.scenarios)


def test_rate_core_declines_unknown_old_unread_and_other_publishers():
    for change in [
        {"kind": "search_lead"},
        {"published_at": None},
        {"published_at": "2026-09-11"},
        {"url": "https://example.org/news"},
        {"title": "Исторический обзор: Банк России принял решение сохранить ключевую ставку"},
        {"date_source": "search_feed"},
        {"text": "Банк России повысил ключевую ставку."},
    ]:
        assert build(sources=[{**SOURCE, **change}]) is None


def test_fresh_policy_pause_does_not_guarantee_the_future_rate_path():
    assert "current_rate_decision_is_not_a_future_commitment" in sentence_issues(
        "Решение ЦБ сохраняет ставку на повышенном уровне на весь плановый период.", bank=True, pause=True
    )
    assert not sentence_issues(
        "Если ключевая ставка сохранится на весь плановый период, проверить условия новых договоров.",
        bank=True,
        pause=True,
    )

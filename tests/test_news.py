"""Synthetic editorial regression set; not an independent relevance benchmark."""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from alfa_agent.api import create_app
from alfa_agent.identity import Principal, current_principal
from alfa_agent.news import news_source
from alfa_agent.news_ranking import company_context, rank_news, score_item, duplicate_story
from alfa_agent.news_sources import (
    clean_news_url,
    parse_feed,
    publication_time,
    company_queries,
    article_matches,
    article_publication,
)
from alfa_agent.research import ResearchRequest, ResearchEngine, assemble_research, ResearchSelection
from alfa_agent.settings import Settings, utcnow
from alfa_agent.storage import NotFound

NOW = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
CONTEXT = {
    "name": "Альфа-Банк",
    "industry": "Денежное посредничество",
    "bank": True,
    "exposure_topics": [],
    "anonymous": False,
    "website": "https://alfabank.ru/",
}


def item(title, *, url="https://www.cbr.ru/press/PR/?file=news.htm", ago=1, read=True):
    return {
        "news_id": "news_" + hashlib.sha256(url.encode()).hexdigest()[:24],
        "url": url,
        "title": title,
        "published_at": (NOW - timedelta(days=ago)).isoformat(),
        "publication_date_raw": "23 October 2026",
        "date_source": "publisher_feed",
        "publisher": "Источник",
        "excerpt": title,
        "read_status": "article_read" if read else "headline_only",
        "kind": "public_page" if read else "search_lead",
        "text": title + ". " + "В публикации сохранён текст источника. " * 8,
    }


def persist_pool(service, cid, items, tenant="demo", status="SUCCEEDED", context=None):
    context = context or service.news.context(cid)
    now = utcnow().isoformat()
    with service.store.connection() as db:
        db.execute(
            "INSERT INTO news_jobs(id,tenant,company_id,role,context_json,status,created_at,finished_at,result_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "job_" + hashlib.sha256((cid + tenant).encode()).hexdigest()[:20],
                tenant,
                cid,
                "cfo",
                json.dumps(context),
                status,
                now,
                now,
                json.dumps({"items": items, "sources": []}),
            ),
        )


def test_feed_namespace_date_and_observed_bing_publisher_link():
    raw = """<rss xmlns:News="https://www.bing.com/news/search"><channel><item>
    <title>Альфа-Банк: &lt;b&gt;результаты&lt;/b&gt;</title>
    <link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.example.com%2Fnews%3Fid%3D1%26utm_source%3Dbing&amp;ref=abc</link>
    <pubDate>Fri, 23 Oct 2026 13:30:00 +0300</pubDate><News:Source>Издатель</News:Source>
    <description><![CDATA[<p>Прибыль и капитал.</p><script>ignore</script>]]></description>
    </item></channel></rss>"""
    parsed = parse_feed(raw, source_id="company_search", label="Bing", feed_url="https://bing.com/news")
    assert len(parsed) == 1 and parsed[0]["url"] == "https://www.example.com/news?id=1"
    assert parsed[0]["publisher"] == "Издатель" and parsed[0]["published_at"] == "2026-10-23T10:30:00+00:00"
    assert parsed[0]["title"] == "Альфа-Банк: результаты" and "ignore" not in parsed[0]["excerpt"]
    assert parsed[0]["kind"] == "search_lead" and parsed[0]["text"] == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "https://u:p@example.com",
        "https://127.0.0.1/x",
        "https://169.254.169.254/",
        "https://localhost/x",
        "https://x.internal/x",
        "javascript:alert(1)",
    ],
)
def test_news_discovery_drops_obvious_unsafe_urls(url):
    assert clean_news_url(url) == ""


def test_dates_are_not_guessed_from_fetch_or_event_title():
    assert publication_time("tomorrow") is None and publication_time("23 October") is None
    assert publication_time("2026-10-23") == "2026-10-23T00:00:00+00:00"
    raw = "<rss><channel><item><title>Решение 23.10.2026</title><link>https://example.com/news</link></item></channel></rss>"
    assert parse_feed(raw, source_id="x", label="x", feed_url="https://x.org")[0]["published_at"] is None
    with pytest.raises(ValueError, match="unsafe_news_xml"):
        parse_feed("<!DOCTYPE rss><rss/>", source_id="x", label="x", feed_url="https://x.org")


def test_company_query_never_uses_anonymous_ids_or_private_context():
    assert company_queries("U_123456", "cfo") == []
    assert company_queries("Синтетическая компания", "cfo") == []
    result = company_queries('Альфа-Банк " site:secret.example', "Секретный долг 999999")
    assert result and "999999" not in repr(result) and "site:" not in repr(result)


def test_cfo_prefers_actual_rate_and_balance_news_over_brand_content():
    rates = item("Банк России сохранил ключевую ставку", url="https://cbr.ru/rates")
    results = item(
        "Альфа-Банк опубликовал финансовые результаты и прибыль", url="https://alfabank.ru/results"
    )
    lounge = item("Альфа-Банк открыл лаунж в аэропорту", url="https://style.rbc.ru/lounge", ago=0.1)
    ranked, _ = rank_news([lounge, rates, results], CONTEXT, "cfo", NOW)
    assert {entry["news_id"] for entry in ranked[:2]} == {results["news_id"], rates["news_id"]}
    assert next(i["score"] for i in ranked if i["news_id"] == rates["news_id"]) > next(
        i["score"] for i in ranked if i["news_id"] == lounge["news_id"]
    )
    assert "promotion_penalty" in ranked[-1]["score_components"]


def test_role_changes_priority_and_explanation_on_same_candidate_pool():
    rates = item("Банк России сохранил ключевую ставку", url="https://cbr.ru/rates")
    launch = item("Альфа-Банк запускает продукт и видеоплатформу", url="https://example.com/launch")
    cfo, _ = rank_news([launch, rates], CONTEXT, "cfo", NOW)
    cco, _ = rank_news([launch, rates], CONTEXT, "cco", NOW)
    assert cfo[0]["news_id"] != cco[0]["news_id"]
    assert "Для CFO" in cfo[0]["reasons"][1] and "Для CCO" in cco[0]["reasons"][1]


def test_educational_cyber_championship_is_not_an_operational_incident():
    title = "Банк России приглашает студентов принять участие в чемпионате Кибербезопасность в финансах"
    assert score_item(item(title), CONTEXT, "coo", NOW)[0] is None
    real = item("Альфа-Банк сообщил о сбое платёжной системы", url="https://alfabank.ru/incident")
    scored, _ = score_item(real, CONTEXT, "coo", NOW)
    assert scored["priority"] == "check_now" and scored["focus"] == "Операционные процессы"


def test_analogous_period_word_does_not_create_a_tax_topic():
    value = item("Альфа-Банк выдал экспресс-гарантии")
    value["excerpt"] = "По сравнению с аналогичным периодом прошлого года объём увеличился."
    scored, _ = score_item(value, CONTEXT, "cfo", NOW)
    assert "inflation" not in scored["topics"]


def test_dates_future_old_unknown_and_conflicting_never_become_fresh():
    fresh = item("Банк России сохранил ключевую ставку", ago=0.2)
    old = item("Банк России сохранил ключевую ставку", url="https://cbr.ru/old", ago=8)
    future = item("Альфа-Банк опубликовал прибыль", url="https://alfabank.ru/future", ago=-1)
    unknown = {**fresh, "news_id": "unknown", "published_at": None}
    conflict = {**fresh, "news_id": "conflict", "read_status": "date_conflict"}
    ranked, excluded = rank_news([fresh, old, future, unknown, conflict], CONTEXT, "cfo", NOW)
    assert [i["news_id"] for i in ranked] == [fresh["news_id"]]
    assert set(excluded) == {"outside_window", "future_date", "unknown_date", "date_conflict"}
    wider, _ = rank_news([old], CONTEXT, "cfo", NOW, 30)
    assert len(wider) == 1


def test_unrelated_company_and_market_analyst_are_not_own_company_events():
    other = item("Альфа-Капитал открыл лаунж", url="https://example.com/capital")
    analyst = item("Альфа-банк сохраняет оценку по рынку для акций ММК", url="https://finmarket.ru/mmk")
    assert score_item(other, CONTEXT, "cfo", NOW)[0] is None
    # No business channel -> excluded; a commentary with a rate channel is retained as commentary.
    analyst["excerpt"] = "Доходность облигаций на фоне ставки."
    ranked, _ = rank_news([analyst], CONTEXT, "cfo", NOW)
    assert ranked[0]["company_relation"] == "market_commentary"
    assert "не событие" in ranked[0]["reasons"][0]


def test_conflicting_or_missing_profile_does_not_add_exposure_boost():
    snapshot = {
        "profile": {
            "features": [
                {"name": "company_name", "text_value": "Альфа-Банк", "status": "observed"},
                {"name": "funding_structure", "status": "conflicting", "text_value": "not verified"},
                {"name": "repricing_gap", "status": "missing"},
            ]
        }
    }
    assert company_context(snapshot)["exposure_topics"] == []
    snapshot["profile"]["features"].append(
        {"name": "repricing_gap", "status": "observed", "text_value": "Переоценка по договору"}
    )
    context = company_context(snapshot)
    scored, _ = score_item(item("Банк России сохранил ключевую ставку"), context, "cfo", NOW)
    assert scored["score_components"]["profile"] == 8


def test_story_dedup_keeps_conflicting_numeric_updates_separate():
    first = item("Банк России сохранил ключевую ставку 14 процентов", url="https://cbr.ru/one")
    copy = item("Банк России сохранил ключевую ставку 14 процентов", url="https://rbc.ru/two")
    update = item("Банк России повысил ключевую ставку 15 процентов", url="https://rbc.ru/three")
    assert duplicate_story(first, copy) and not duplicate_story(first, update)
    ranked, excluded = rank_news([copy, first, update], CONTEXT, "cfo", NOW)
    assert len(ranked) == 2 and excluded["duplicate_story"] == 1
    assert next(i for i in ranked if i["url"] == first["url"])["related_sources"][0]["url"] == copy["url"]


def test_homepage_or_short_page_cannot_be_marked_as_article_read():
    source = item("Альфа-Банк опубликовал финансовые результаты")
    assert not article_matches(source, {"title": "Главная", "text": "Сайт банка" * 10})
    assert article_matches(source, {"title": source["title"], "text": source["text"]})


def test_news_refresh_is_coalesced_and_role_switch_uses_same_pool(service):
    cid = service.workspace.create_company("Альфа-Банк")["company_id"]
    first = service.news.refresh(cid, "cfo")
    again = service.news.refresh(cid, "coo", force=True)
    assert first["job_id"] == again["job_id"]
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM news_jobs").fetchone()[0] == 1


def test_news_isolation_applies_to_results_research_selection_and_feedback(service):
    a = Principal("user_a", "", "Тест Первый", "Альфа-Банк", "CFO", "cfo")
    b = Principal("user_b", "", "Тест Второй", "Альфа-Банк", "CFO", "cfo")
    token = current_principal.set(a)
    try:
        cid = service.workspace.create_company("Альфа-Банк")["company_id"]
        news = item("Банк России сохранил ключевую ставку", ago=0)
        persist_pool(service, cid, [news], tenant=a.user_id)
        assert service.news.item(cid, news["news_id"])["title"] == news["title"]
        current_principal.set(b)
        with pytest.raises(Exception, match="company_not_accessible"):
            service.news.get(cid)
        other = service.workspace.create_company("Альфа-Банк")["company_id"]
        with pytest.raises(NotFound):
            service.news.item(other, news["news_id"])
        with pytest.raises(NotFound):
            service.news.feedback(other, news["news_id"], "irrelevant")
    finally:
        current_principal.reset(token)


def test_selected_news_is_frozen_from_server_not_trusted_client_content(service):
    cid = service.workspace.create_company("Альфа-Банк")["company_id"]
    news = item("Банк России сохранил ключевую ставку", ago=0)
    persist_pool(service, cid, [news])
    job = service.research.create(
        ResearchRequest(
            company_id=cid,
            question="Что проверить CFO по этой новости?",
            job_title="CFO",
            news_item_id=news["news_id"],
            public_search=False,
        ),
        "news-selection",
    )
    source = next(s for s in job["sources"] if s.get("news_item"))
    assert source["text"] == news["text"] and source["provided_for_current_question"]
    assert source["kind"] == "public_page"
    with pytest.raises(NotFound):
        service.research.create(
            ResearchRequest(
                company_id=cid, question="Что проверить CFO по этой новости?", news_item_id="news_" + "0" * 24
            ),
            "bad-news",
        )


def test_selected_fresh_news_cannot_be_replaced_with_old_bank_view():
    ledger = [
        dict(
            anchor_id="a1",
            source_id="news_" + "1" * 24,
            quote="Банк России сохранил ключевую ставку на прежнем уровне.",
            kind="public_page",
            news_item=True,
            published_at="2026-10-23",
        ),
        dict(
            anchor_id="a2",
            source_id="bank",
            quote="Ставка без изменения; обзор сентября.",
            kind="bank_claim",
            claim_type="ASSESSMENT",
            published_at="2026-09-11",
        ),
    ]
    selection = ResearchSelection.model_validate(
        {"findings": [{"mechanism": "bank_alm", "anchor_ids": ["a2"], "priority": "check_now"}]}
    )
    with pytest.raises(ValueError, match="selected_news_must_ground_interpretation"):
        assemble_research(
            selection,
            ledger,
            {"bank_company": True, "news_item_id": "news_" + "1" * 24},
            {"profile": {"features": []}},
        )
    selection.findings[0].anchor_ids = ["a1"]
    draft = assemble_research(
        selection,
        ledger,
        {"bank_company": True, "news_item_id": "news_" + "1" * 24},
        {"profile": {"features": []}},
    )
    assert any(f.kind == "source_fact" for f in draft.findings)
    assert len(next(f for f in draft.findings if f.kind == "source_fact").citations[0].quote.split()) <= 24


def test_feed_collector_reads_real_article_and_cache_not_model(service):
    title = "Банк России сохранил ключевую ставку"
    date = utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    xml = f"<rss><channel><item><title>{title}</title><link>https://www.cbr.ru/news/one</link><pubDate>{date}</pubDate></item></channel></rss>"
    context = {**CONTEXT, "anonymous": True}
    calls = []

    def read(url):
        calls.append(url)
        return {"text": xml, "url": url}

    def article(url):
        return {
            "url": url,
            "publisher": "cbr.ru",
            "title": title,
            "published_at": utcnow().date().isoformat(),
            "text": title + ". " + "Сохранены условия денежно-кредитной политики. " * 10,
        }

    with (
        patch("alfa_agent.news.read_public", side_effect=read),
        patch("alfa_agent.news.page_source", side_effect=article),
    ):
        result = asyncio.run(service.news.collect(context, "cfo"))
        assert (
            result["no_llm_used"]
            and len(result["items"]) == 1
            and result["items"][0]["read_status"] == "article_read"
        )
        assert all("bing.com" not in url for url in calls)
        asyncio.run(service.news.collect(context, "cfo"))
        assert len(calls) == 4
    assert news_source(result["items"][0])["kind"] == "public_page"


def test_news_api_rejects_arbitrary_topic_and_bounds_period(tmp_path):
    from pathlib import Path
    import shutil

    (tmp_path / "data").mkdir()
    shutil.copy2(Path(__file__).parents[1] / "data/demo_bundle.json", tmp_path / "data/demo_bundle.json")
    with TestClient(create_app(Settings(root=tmp_path))) as client:
        token = client.app.state.token
        headers = {"x-app-token": token}
        cid = client.post("/api/companies", headers=headers, json={"name": "Альфа-Банк"}).json()["company_id"]
        assert client.get(f"/api/companies/{cid}/news?days=999", headers=headers).status_code == 422
        assert (
            client.get(f"/api/companies/{cid}/news?role=секретные_данные", headers=headers).status_code == 422
        )
        r = client.post(
            f"/api/companies/{cid}/news/refresh", headers=headers, json={"role": "cfo", "force": False}
        )
        assert r.status_code == 202 and r.json()["status"] == "QUEUED"


def test_author_calendar_date_survives_midnight_timezone_conversion():
    raw = "<rss><channel><item><title>Решение по ставке</title><link>https://cbr.ru/news</link><pubDate>Fri, 23 Oct 2026 00:30:00 +0300</pubDate></item></channel></rss>"
    value = parse_feed(raw, source_id="cbr", label="ЦБ", feed_url="https://cbr.ru/rss")[0]
    assert value["published_at"].startswith("2026-10-22T21:30")
    assert value["publication_date"] == "2026-10-23"
    assert article_publication(value, {"published_at": "2026-10-23"}) == {}
    with pytest.raises(ValueError, match="publisher_date_conflict"):
        article_publication(value, {"published_at": "2026-10-20"})


def test_publisher_date_overrides_search_index_date_with_provenance():
    value = item("Финансовые результаты")
    value["date_source"] = "search_feed"
    metadata = article_publication(value, {"published_at": "2026-10-22"})
    assert metadata["published_at"].startswith("2026-10-22")
    assert metadata["date_source"] == "publisher_article"
    assert metadata["search_published_at"] == value["published_at"]


def test_dedup_preserves_opposite_direction_and_numeric_sign():
    first = item(
        "Альфа-Банк повысил прогноз изменения прибыли на +1 процент в следующем году",
        url="https://example.com/a",
    )
    second = item(
        "Альфа-Банк снизил прогноз изменения прибыли на -1 процент в следующем году",
        url="https://example.com/b",
    )
    assert not duplicate_story(first, second)


def test_one_failed_refresh_does_not_prevent_next_news_job(service):
    a = service.workspace.create_company("Альфа-Банк")["company_id"]
    b = service.workspace.create_company("Другой банк")["company_id"]
    service.news.refresh(a)
    service.news.refresh(b)

    async def collect(context, role):
        if context["name"] == "Альфа-Банк":
            raise RuntimeError("synthetic parser failure")
        return {"items": [], "sources": [{"name": "Test", "status": "ok", "count": 0}]}

    with patch.object(service.news, "collect", side_effect=collect):
        asyncio.run(service.news.once())
        asyncio.run(service.news.once())
    assert service.news.get(a)["status"] == "FAILED"
    assert service.news.get(a)["error_code"] == "news_refresh_failed"
    assert service.news.get(b)["status"] == "SUCCEEDED"


def test_unknown_financial_values_and_negated_export_do_not_create_exposure():
    snapshot = {
        "profile": {
            "features": [
                {"name": "company_name", "text_value": "Альфа-Банк", "status": "observed"},
                {"name": "repricing_gap", "status": "observed", "text_value": "Данные не предоставлены"},
            ]
        },
        "context": {"description": "У компании нет экспортных операций"},
    }
    assert company_context(snapshot)["exposure_topics"] == []


def test_generic_fresh_research_cannot_ignore_available_current_news():
    ledger = [
        dict(
            anchor_id="a1",
            source_id="news_fresh",
            quote="Банк России сохранил ключевую ставку на прежнем уровне.",
            kind="public_page",
            news_item=True,
            provided_for_current_question=True,
            published_at="2026-10-23",
        ),
        dict(
            anchor_id="a2",
            source_id="bank_old",
            quote="Ставка без изменения; старый обзор.",
            kind="bank_claim",
            claim_type="ASSESSMENT",
            published_at="2026-09-11",
        ),
    ]
    selection = ResearchSelection.model_validate(
        {"findings": [{"mechanism": "bank_alm", "anchor_ids": ["a2"], "priority": "check_now"}]}
    )
    request = {"bank_company": True, "fresh_news_requested": True}
    with pytest.raises(ValueError, match="fresh_news_must_ground_interpretation"):
        assemble_research(selection, ledger, request, {"profile": {"features": []}})
    selection.findings[0].anchor_ids = ["a1"]
    assert any(
        f.kind == "source_fact"
        for f in assemble_research(selection, ledger, request, {"profile": {"features": []}}).findings
    )


def test_secondary_feed_failure_uses_bounded_public_fallback(service):
    from alfa_agent.public_sources import PublicSourceError

    calls = []
    title = "Банк России сохранил ключевую ставку"
    date = utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    xml = f"<rss><channel><item><title>{title}</title><link>https://cbr.ru/policy</link><pubDate>{date}</pubDate></item></channel></rss>"

    def read(url):
        calls.append(url)
        if "rbc.ru" in url:
            raise PublicSourceError("public_page_unavailable")
        return {"text": xml, "url": url}

    article = {
        "title": title,
        "text": title + ". " + "В источнике есть условия денежно-кредитной политики. " * 8,
        "url": "https://cbr.ru/policy",
        "publisher": "cbr.ru",
        "published_at": utcnow().date().isoformat(),
    }
    with (
        patch("alfa_agent.news.read_public", side_effect=read),
        patch("alfa_agent.news.page_source", return_value=article),
    ):
        result = asyncio.run(service.news.collect({**CONTEXT, "anonymous": True}, "cfo"))
    assert len(calls) == 5 and calls[-1] == "https://www.interfax.ru/rss.asp"
    assert result["sources"][-1]["status"] == "ok"
    assert any(s["status"] == "unavailable" for s in result["sources"])


def test_reading_selected_news_does_not_accept_an_unrelated_homepage(service):
    cid = service.workspace.create_company("Альфа-Банк")["company_id"]
    value = item("Альфа-Банк опубликовал финансовые результаты", url="https://example.com/news", read=False)
    persist_pool(service, cid, [value])
    job = service.research.create(
        ResearchRequest(
            company_id=cid, question="Что проверить CFO по этой новости?", news_item_id=value["news_id"]
        ),
        "mismatched-article",
    )

    class Gateway:
        async def complete(self, run_id, stage, payload, schema, repair=None):
            if stage == "research_plan":
                return json.dumps(
                    {
                        "rationale": "Prepare a conditional result",
                        "actions": [{"tool": "write", "reason": "Use available material"}],
                    }
                )
            if stage == "research_write":
                anchor = next(a for a in payload["evidence_ledger"] if a["kind"] == "bank_claim")
                return json.dumps(
                    {
                        "findings": [
                            {
                                "mechanism": "bank_alm",
                                "anchor_ids": [anchor["anchor_id"]],
                                "priority": "check_now",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "approved": True,
                    "issues": [],
                    "scope_note": "Synthetic verifier for an evidence rejection test",
                }
            )

    unrelated = {
        "title": "Главная страница",
        "publisher": "example.com",
        "url": "https://example.com/",
        "kind": "public_page",
        "text": "Общая навигация по сайту. " * 30,
        "published_at": None,
    }
    with patch("alfa_agent.research.page_source", return_value=unrelated):
        asyncio.run(ResearchEngine(service.research, Gateway()).once())
    result = service.research.get(job["run_id"])
    assert result["result"] is None
    assert any(
        t["event"] == "tool_completed" and t["data"]["error"] == "article_content_mismatch"
        for t in result["trace"]
    )
    assert not any(
        s["source_id"] == value["news_id"] and s["kind"] == "public_page" for s in result["sources"]
    )


def test_failed_auto_refresh_becomes_retryable_after_cooldown(service):
    cid = service.workspace.create_company("Альфа-Банк")["company_id"]
    persist_pool(service, cid, [], status="FAILED")
    assert not service.news.get(cid)["refresh_due"]
    old = service.news._latest("demo", cid)["id"]
    with service.store.connection() as db:
        db.execute(
            "UPDATE news_jobs SET finished_at=? WHERE id=?",
            ((utcnow() - timedelta(seconds=61)).isoformat(), old),
        )
    assert service.news.get(cid)["refresh_due"]
    assert service.news.refresh(cid, force=False)["job_id"] != old


def test_press_conference_announcement_is_below_published_policy_result():
    result = item("Банк России принял решение сохранить ключевую ставку", url="https://cbr.ru/decision")
    preview = item(
        "23 октября состоится пресс-конференция по денежно-кредитной политике",
        url="https://cbr.ru/announcement",
        ago=0.1,
    )
    ranked, _ = rank_news([preview, result], CONTEXT, "cfo", NOW)
    assert ranked[0]["news_id"] == result["news_id"]
    announcement = next(i for i in ranked if i["news_id"] == preview["news_id"])
    assert announcement["priority"] != "check_now"
    assert any("Это анонс" in reason for reason in announcement["reasons"])


def test_other_bank_license_loss_does_not_imply_alfa_counterparty_exposure():
    other = item("Отозвана лицензия у НКО Другой Банк", url="https://cbr.ru/other")
    own = item("Отозвана лицензия у Альфа-Банка", url="https://cbr.ru/own")
    unrelated, _ = score_item(other, CONTEXT, "cfo", NOW)
    direct, _ = score_item(own, CONTEXT, "cfo", NOW)
    assert unrelated["score"] < direct["score"]
    assert unrelated["priority"] != "check_now"
    assert any("экспозиция" in r for r in unrelated["reasons"])

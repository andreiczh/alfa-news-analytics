"""P0 acceptance: relevance, explicit periods, grounded cards and context refresh.

All company inputs and gateway responses here are synthetic protocol fixtures.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alfa_agent.api import create_app
from alfa_agent.bank_library import BankLibrary, bank_query_terms, terms
from alfa_agent.company_dialogue import ContextConfirm
from alfa_agent.decision_support import news_interpretation
from alfa_agent.news_ranking import company_context
from alfa_agent.periods import period_intent
from alfa_agent.storage import Conflict
from test_research import ResearchEngine, ResearchGateway, request_for
from test_workspace_search import ready_file, news, pool


@pytest.mark.parametrize(
    "query",
    [
        "ксилофоноградквазитест",
        "ксилофоноградквазитест за последние 90 дней",
        "ультрафиолетовая керамика",
        "Что покажи анализ банка для компании CFO",
        "Новости материалы Альфа-Банка для нашей компании",
        "",
        "CFO",
    ],
)
def test_bank_search_never_promotes_zero_content_match(query):
    assert BankLibrary(Path(__file__).parents[1]).search(query) == []


@pytest.mark.parametrize(
    "query", ["ликвидность фондирование", "инфляция потребление", "золото", "ключевая ставка"]
)
def test_bank_search_keeps_relevant_verbatim_dated_fragments(query):
    bank = BankLibrary(Path(__file__).parents[1])
    hits = bank.search(query)
    assert hits
    for hit in hits:
        assert terms(hit["text"]) & bank_query_terms(query)
        doc = next(d for d in bank.documents if d["document_id"] == hit["document_id"])
        assert hit["published_at"] == doc["published_at"]
        assert any(hit["text"] in p["text"] for p in doc["pages"])


@pytest.mark.parametrize(
    "question,news,planning,conflict",
    [
        ("Что изменилось за 90 дней?", 90, None, True),
        ("Что изменилось за последние три месяца?", 90, None, True),
        ("Что изменилось за неделю?", 7, None, False),
        ("События за последние 30 дней и план на 90 дней", 30, 90, True),
        ("Сценарий на ближайшие 90 дней", None, 90, True),
        ("План на год", None, 365, True),
        ("Что значит ставка для бюджета?", None, None, False),
        ("Что изменилось за 14 дней?", 14, None, True),
        ("План до конца года", None, None, True),
        ("События за 2026 год", None, None, True),
    ],
)
def test_period_hints_separate_lookback_from_planning_without_override(question, news, planning, conflict):
    value = period_intent(question)
    assert value["detected_news_days"] == news
    assert value["detected_planning_days"] == planning
    assert value["needs_confirmation"] is conflict
    assert value["news_days"] == 7 and value["planning_days"] == 30


def test_period_api_and_direct_research_cannot_silently_ignore_conflict(service):
    app = create_app(service.settings)
    with TestClient(app) as client:
        client.headers["x-app-token"] = client.app.state.token
        service = client.app.state.service
        data = client.get(
            "/api/research/periods",
            params={"q": "Фондирование за 90 дней, план на 30 дней", "news_days": 7, "planning_days": 30},
        ).json()
        assert data["needs_confirmation"] and data["detected_news_days"] == 90
        assert (
            client.get("/api/research/periods", params={"q": "ставка", "news_days": 200}).status_code == 422
        )
        request = request_for(service).model_copy(
            update={"question": "Как изменилось фондирование за 90 дней?"}
        )
        with pytest.raises(Conflict, match="research_period_confirmation_required"):
            service.research.create(request, "period-unconfirmed")
        confirmed = request.model_copy(update={"periods_confirmed": True})
        result = service.research.create(confirmed, "period-confirmed")
        assert result["news_days"] == 7  # Explicit choice, not a silent reinterpretation.
        assert service.research._row(result["run_id"])["input"]["periods"]["needs_confirmation"]
        inferred = request.model_copy(update={"news_days": 90})
        result = service.research.create(inferred, "period-matched")
        assert result["news_days"] == 90 and result["planning_days"] == 30


def test_current_refresh_replaces_internal_context_preserves_external_and_parent(service):
    request = request_for(service)
    cid = request.company_id
    uploaded = ready_file(
        service,
        cid,
        "Синтетический старый договор: фондирование зависит от плавающей ставки. Старый маркер ARCHIVE_ONLY.",
    )
    parent = service.research.create(request, "refresh-parent")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    parent = service.research.get(parent["run_id"])
    frozen = service.research._row(parent["run_id"])
    assert any(s["kind"] == "company_evidence" for s in parent["sources"])
    service.workspace.set_file_active(cid, uploaded["document_id"], False)
    service.context_dialogue.save_fields(
        cid,
        ContextConfirm(
            expected_revision=0,
            fields=[
                {
                    "key": "funding",
                    "category": "finance",
                    "label": "Финансирование",
                    "value": "Синтетические актуальные условия: фиксированная ставка; маркер CURRENT_ONLY.",
                }
            ],
        ),
    )
    original = service.research.create(
        request.model_copy(update={"parent_id": parent["run_id"]}), "refresh-original"
    )
    current = service.research.create(
        request.model_copy(update={"parent_id": parent["run_id"], "context_mode": "current"}),
        "refresh-current",
    )
    assert original["profile_snapshot_id"] == parent["profile_snapshot_id"]
    assert current["profile_snapshot_id"] != parent["profile_snapshot_id"]
    assert current["context_version"]["context_revision"] > parent["context_version"]["context_revision"]
    assert any(s["kind"] == "company_evidence" for s in original["sources"])
    assert not any(s["kind"] == "company_evidence" for s in current["sources"])
    assert {s["source_id"] for s in parent["sources"] if s["kind"] == "user_news"} <= {
        s["source_id"] for s in current["sources"]
    }
    current_row = service.research._row(current["run_id"])
    assert "previous_summary" not in current_row["input"]
    snapshot = service.workspace.load_snapshot(current["profile_snapshot_id"], cid)
    assert "CURRENT_ONLY" in json.dumps(snapshot, ensure_ascii=False)
    assert uploaded["document_id"] not in snapshot.get("document_chunks", {})
    assert service.research._row(parent["run_id"]) == frozen
    assert current["parent_id"] == parent["run_id"] and current["context_mode"] == "current"
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())  # Original continuation.
    gateway = ResearchGateway()
    asyncio.run(ResearchEngine(service.research, gateway).once())  # Current continuation.
    prompt_context = json.dumps(gateway.payloads, ensure_ascii=False)
    assert "CURRENT_ONLY" in prompt_context
    assert "ARCHIVE_ONLY" not in prompt_context
    assert service.research._row(parent["run_id"]) == frozen
    # Retrying the refresh keeps its first snapshot even if the profile changes again.
    service.workspace.save_context(cid, "Новые вводные после постановки задачи.", "")
    retry = service.research.create(
        request.model_copy(update={"parent_id": parent["run_id"], "context_mode": "current"}),
        "refresh-current",
    )
    assert retry["profile_snapshot_id"] == current["profile_snapshot_id"]


def test_current_refresh_waits_for_processing_but_frozen_followup_can_continue(service):
    request = request_for(service)
    parent = service.research.create(request, "processing-parent")
    service.workspace.add_file(request.company_id, "new.txt", b"Synthetic queued company file.")
    with pytest.raises(Conflict, match="company_documents_processing"):
        service.research.create(
            request.model_copy(update={"parent_id": parent["run_id"], "context_mode": "current"}),
            "processing-current",
        )
    assert (
        service.research.create(
            request.model_copy(update={"parent_id": parent["run_id"]}), "processing-original"
        )["context_mode"]
        == "original"
    )


def test_news_tool_honors_requested_window_and_frozen_context(service, monkeypatch):
    cid = service.workspace.create_company("Синтетический Альфа-Банк")["company_id"]
    service.context_dialogue.save_fields(
        cid,
        ContextConfirm(
            expected_revision=0,
            fields=[
                {
                    "key": "funding",
                    "category": "finance",
                    "label": "Фондирование",
                    "value": "Синтетические данные: депозиты и ставки фондирования.",
                }
            ],
        ),
    )
    snapshot = service.workspace.snapshot(cid, service.corpus, persist=True)
    pool(service, cid, [news("Синтетический Альфа-Банк разместил облигации для фондирования", age=20)])
    job_id = service.news._latest(service.tenant, cid, success_only=True)["id"]
    service.context_dialogue.save_fields(
        cid,
        ContextConfirm(
            expected_revision=1,
            fields=[
                {
                    "key": "funding",
                    "category": "finance",
                    "label": "Новые приоритеты",
                    "value": "Синтетические новые вводные о фондировании, отсутствовавшие в снимке.",
                }
            ],
        ),
    )
    observed = []

    def enqueue(tenant, company_id, context, role):
        observed.append(context)
        return job_id

    monkeypatch.setattr(service.news, "_enqueue", enqueue)
    sources = asyncio.run(service.news.for_research(cid, service.tenant, snapshot, "cfo", "rates", days=30))
    assert len(sources) == 1
    assert not asyncio.run(service.news.for_research(cid, service.tenant, snapshot, "cfo", "rates", days=7))
    assert all(c["context_revision"] == snapshot["context"]["revision"] for c in observed)
    assert observed[0]["context_revision"] != service.workspace.context(cid)["revision"]


def decision_fixture(bank=False):
    profile = {
        "features": [
            {
                "name": "company_name",
                "text_value": "Синтетический банк" if bank else "Синтетический завод",
                "status": "provided",
            },
            {
                "name": "repricing_gap" if bank else "rate_type",
                "text_value": "Пассивы 30 дней, активы 90 дней"
                if bank
                else "Фиксированная ставка до погашения",
                "status": "provided",
                "source_refs": [],
            },
        ]
    }
    item = {
        "title": "Синтетическая новость: ЦБ сохранил ключевую ставку",
        "topics": ["rates"],
        "primary_topic": "rates",
        "read_status": "article_read",
        "company_relation": "macro",
        "reasons": ["Связано с вашим приоритетом: Ставка"],
        "matched_priorities": ["Ставка"],
    }
    return item, company_context({"profile": profile, "context": {"fields": {}}})


@pytest.mark.parametrize(
    "bank,role", [(False, "cfo"), (False, "treasurer"), (True, "cfo"), (True, "treasurer"), (False, "cmo")]
)
def test_decision_card_has_actual_company_factor_conditional_effect_and_specific_action(bank, role):
    item, context = decision_fixture(bank)
    result = news_interpretation(item, context, role)
    assert result["company_factor"]["value"] in {
        "Фиксированная ставка до погашения",
        "Пассивы 30 дней, активы 90 дней",
    }
    assert result["company_factor"]["origin"] == "company_profile"
    assert result["why"].casefold().count("приоритет") == 1
    assert result["condition"] and result["action"]
    assert "вырастет" not in result["impact"] and "снизится" not in result["impact"]
    assert result["event_semantics"] == "rate_unchanged_report"
    if role != "cmo":
        assert "новое изменение ключевой ставки не объявлено" in result["impact"]
        assert "дорожают" not in result["impact"]


def test_unknown_unread_conflicting_and_query_only_news_do_not_gain_financial_certainty():
    item, context = decision_fixture()
    item["read_status"] = "headline_only"
    result = news_interpretation(item, context, "cfo")
    assert "не определено" in result["impact"] and "первоисточник" in result["action"]
    item["read_status"] = "article_read"
    item["company_relation"] = "query_only"
    result = news_interpretation(item, context, "cfo")
    assert result["company_factor"] is None and "не подтверждена" in result["impact"]
    item["company_relation"] = "macro"
    result = news_interpretation(
        item,
        company_context(
            {
                "profile": {
                    "features": [
                        {"name": "rate_type", "text_value": "Плавающая ставка", "status": "conflicting"}
                    ]
                }
            }
        ),
        "cfo",
    )
    assert result["company_factor"] is None
    assert result["missing_company_data"]


def test_confirmed_free_text_rate_terms_become_visible_factors_with_synthetic_provenance():
    item, _ = decision_fixture()
    value = "Плавающая ставка по новому кредиту, переоценка через 30 дней."
    snapshot = {
        "context": {
            "fields": {
                "rate_type": {
                    "key": "rate_type",
                    "label": "Ставка по долгу",
                    "category": "finance",
                    "value": value,
                    "synthetic": True,
                }
            }
        }
    }
    result = news_interpretation(item, company_context(snapshot), "cfo")
    assert result["company_factor"]["value"] == value
    assert result["company_factor"]["synthetic"]
    assert result["company_factor"]["origin"] == "user_confirmed"


def test_conflicting_document_and_confirmed_factor_are_not_silently_resolved():
    item, _ = decision_fixture()
    snapshot = {
        "profile": {
            "features": [
                {"name": "rate_type", "text_value": "Фиксированная ставка", "status": "provided"},
            ]
        },
        "context": {
            "fields": {
                "rate_type": {
                    "key": "rate_type",
                    "label": "Тип ставки",
                    "category": "finance",
                    "value": "Плавающая ставка",
                }
            }
        },
    }
    value = news_interpretation(item, company_context(snapshot), "cfo")
    assert value["company_factor"] is None
    assert value["company_factor_conflicts"] and "противоречащие" in value["action"]
    snapshot["context"]["fields"]["rate_type"]["value"] = "Фиксированная ставка"
    value = news_interpretation(item, company_context(snapshot), "cfo")
    assert value["company_factor"] and not value["company_factor_conflicts"]

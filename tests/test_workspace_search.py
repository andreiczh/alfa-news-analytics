"""Search and saved-source boundaries; fixtures are explicitly synthetic."""

from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from alfa_agent.api import create_app
from alfa_agent.corpus import AccessDenied
from alfa_agent.domain import DocumentRef
from alfa_agent.identity import current_principal
from alfa_agent.research import ResearchRequest
from alfa_agent.settings import utcnow
from alfa_agent.storage import NotFound, new_id


def news(title, *, age=1, read=True, suffix="1"):
    text = title + ". Это синтетический текст для проверки поиска, не реальная новость."
    return {
        "news_id": "news_" + hashlib.sha256(suffix.encode()).hexdigest()[:24],
        "url": "https://example.org/test-news/" + suffix,
        "title": title,
        "publisher": "Синтетический издатель",
        "published_at": (utcnow() - timedelta(days=age)).isoformat(),
        "date_source": "publisher_feed",
        "excerpt": text,
        "text": text if read else "",
        "read_status": "article_read" if read else "headline_only",
    }


def pool(service, cid, items, *, tenant=None, order=0):
    created = (utcnow() + timedelta(seconds=order)).isoformat()
    with service.store.connection() as db:
        db.execute(
            "INSERT INTO news_jobs(id,tenant,company_id,role,context_json,status,created_at,finished_at,result_json) VALUES(?,?,?,?,?,'SUCCEEDED',?,?,?)",
            (
                new_id("newsjob"),
                tenant or service.tenant,
                cid,
                "cfo",
                json.dumps(service.news.context(cid)),
                created,
                created,
                json.dumps({"items": items, "sources": []}),
            ),
        )


def ready_file(service, cid, text):
    file, _ = service.workspace.add_file(cid, "synthetic-company.txt", text.encode())
    job = service.workspace.claim_document()
    doc = DocumentRef(
        document_id=file["document_id"],
        source_kind="company_upload",
        title="Синтетический документ",
        publisher="Тестовая компания",
        published_at=None,
        publication_date_raw=None,
        available_at=utcnow(),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        tenant_company_id=cid,
    )
    service.workspace.finish_document(
        job, doc, [{"id": "chunk-1", "text": text, "page": 2, "locator": "Раздел фондирования"}], [], [], []
    )
    return file


def test_search_connects_news_with_exact_private_fragments_and_honest_empty(service):
    cid = service.workspace.create_company("Синтетический Альфа-Банк")["company_id"]
    text = "Синтетический план: структура фондирования включает депозиты. Ликвидность контролируется казначейством."
    ready_file(service, cid, text)
    pool(service, cid, [news("Синтетический Альфа-Банк разместил облигации для фондирования")])
    result = service.workspace_search.search(cid, "Фондирование и ликвидность")
    assert result["news"] and result["company"]
    assert result["company"][0]["quote"] in text
    assert result["company"][0]["page"] == 2
    assert "text" not in result["news"][0]
    empty = service.workspace_search.search(cid, "ультрафиолетовая керамика")
    assert not empty["news"] and not empty["company"] and not empty["bank"]
    assert service.workspace_search.search(cid, "CFO")["empty_query_terms"]


def test_explicit_query_retrieves_outside_normal_top_twenty_without_inventing_company_link(service):
    cid = service.workspace.create_company("Синтетический Альфа-Банк")["company_id"]
    unrelated = news("Завод поставщика Керамика расширяет обжиг печей", suffix="target")
    items = [news(f"Синтетический Альфа-Банк увеличил прибыль {n}", suffix=str(n)) for n in range(30)] + [
        unrelated
    ]
    pool(service, cid, items)
    result = service.workspace_search.search(cid, "обжиг печей")
    assert [i["news_id"] for i in result["news"]] == [unrelated["news_id"]]
    assert result["news"][0]["company_relation"] == "query_only"
    assert "не подтверждена" in result["news"][0]["decision"]["why"]


def test_search_keeps_date_entity_and_unread_guards(service):
    cid = service.workspace.create_company("Северсталь")["company_id"]
    valid = news("Северсталь объявила об инвестициях в производство", read=False, suffix="valid")
    items = [
        valid,
        news("Северсталь сыграла хоккейный матч", suffix="sport"),
        news("Северсталь объявила об инвестициях вчера", age=-3, suffix="future"),
        news("Северсталь объявила об инвестициях давно", age=60, suffix="old"),
    ]
    no_date = news("Северсталь объявила об инвестициях без даты", suffix="nodate")
    no_date["published_at"] = None
    items.append(no_date)
    pool(service, cid, items)
    result = service.workspace_search.search(cid, "Северсталь", days=7)
    assert [i["news_id"] for i in result["news"]] == [valid["news_id"]]
    assert result["news"][0]["read_status"] == "headline_only"


def test_excluded_file_does_not_return_in_search_and_can_be_restored(service):
    cid = service.workspace.create_company("Синтетическая компания")["company_id"]
    file = ready_file(service, cid, "План: фондирование и ликвидность для синтетической компании.")
    assert service.workspace_search.search(cid, "ликвидность", scope="company")["company"]
    service.workspace.set_file_active(cid, file["document_id"], False)
    assert not service.workspace_search.search(cid, "ликвидность", scope="company")["company"]
    service.workspace.set_file_active(cid, file["document_id"], True)
    assert service.workspace_search.search(cid, "ликвидность", scope="company")["company"]


def test_saved_news_survives_pool_rollover_and_retains_original_evidence(service):
    cid = service.workspace.create_company("Синтетический банк")["company_id"]
    original = news("Синтетический банк разместил облигации", suffix="archive")
    pool(service, cid, [original])
    service.news.save_item(cid, original["news_id"], True)
    service.news.save_item(cid, original["news_id"], True)
    for order in range(1, 10):
        pool(service, cid, [], order=order)
    saved = service.news.saved_items(cid)
    assert saved["total"] == 1 and len(saved["items"]) == 1
    assert saved["items"][0]["published_at"] == original["published_at"]
    assert "text" not in saved["items"][0]
    assert service.news.item(cid, original["news_id"])["text"] == original["text"]
    job = service.research.create(
        ResearchRequest(
            company_id=cid,
            question="Разберите сохранённое событие для компании",
            news_item_id=original["news_id"],
            public_search=False,
        ),
        "saved-event",
    )
    source = next(s for s in job["sources"] if s["source_id"] == original["news_id"])
    assert source["text"] == original["text"] and source["published_at"] == original["published_at"]
    service.news.save_item(cid, original["news_id"], False)
    with pytest.raises(NotFound):
        service.news.item(cid, original["news_id"])


def test_search_and_saved_news_are_scoped_to_user_even_in_shared_workspace(service):
    a = service.accounts.register(
        "search-a", "synthetic-test-password-a", "Первый Пользователь", "Одинаковая компания", "CFO"
    )
    b = service.accounts.register(
        "search-b", "synthetic-test-password-b", "Второй Пользователь", "Одинаковая компания", "CFO"
    )
    marker = current_principal.set(a)
    original = news("Одинаковая компания разместила облигации")
    try:
        pool(service, a.company_id, [original])
        service.news.save_item(a.company_id, original["news_id"], True)
    finally:
        current_principal.reset(marker)
    marker = current_principal.set(b)
    try:
        with pytest.raises(AccessDenied):
            service.workspace_search.search(a.company_id, "облигации")
        with service.store.connection() as db:
            db.execute(
                "INSERT INTO company_members VALUES(?,?,?,1,?)",
                (a.company_id, b.user_id, "viewer", utcnow().isoformat()),
            )
        assert service.news.saved_items(a.company_id)["total"] == 0
        assert not service.workspace_search.search(a.company_id, "облигации")["news"]
        with pytest.raises(NotFound):
            service.news.item(a.company_id, original["news_id"])
    finally:
        current_principal.reset(marker)


def test_reports_pagination_and_saved_filter_include_older_reports(service):
    cid = service.workspace.create_company("Синтетическая компания отчётов")["company_id"]
    ids = []
    for n in range(5):
        job = service.research.create(
            ResearchRequest(company_id=cid, question=f"Синтетический вопрос номер {n}", public_search=False),
            f"report-{n}",
        )
        service.research.cancel(job["run_id"])
        ids.append(job["run_id"])
    service.research.set_favorite(ids[0], True)
    first = service.research.history(cid, limit=2)
    second = service.research.history(cid, limit=2, offset=2)
    assert not ({j["run_id"] for j in first} & {j["run_id"] for j in second})
    saved = service.research.history(cid, saved_only=True, limit=2)
    assert len(saved) == 1 and saved[0]["run_id"] == ids[0]
    assert all("has_result" in j and "role" in j and "parent_id" in j for j in first)


def test_api_query_bounds_and_private_file_search(service):
    app = create_app(service.settings)
    with TestClient(app) as client:
        client.headers["X-App-Token"] = app.state.token
        cid = client.post("/api/companies", json={"name": "Синтетический банк API"}).json()["company_id"]
        assert client.get(f"/api/companies/{cid}/search", params={"q": "ставка"}).status_code == 200
        assert client.get(f"/api/companies/{cid}/search", params={"q": "x"}).status_code == 422
        assert (
            client.get(f"/api/companies/{cid}/search", params={"q": "ставка", "days": 91}).status_code == 422
        )
        assert client.get(f"/api/companies/{cid}/news/saved", params={"limit": 51}).status_code == 422
        assert (
            client.get("/api/research", params={"company_id": cid, "limit": 2, "saved_only": True}).json()
            == []
        )


def test_search_news_references_are_server_resolved_for_the_research(service):
    cid = service.workspace.create_company("Синтетический банк источников")["company_id"]
    items = [
        news("Синтетическое фондирование банка", suffix="seed-one"),
        news("Синтетическая ликвидность банка", suffix="seed-two"),
    ]
    pool(service, cid, items)
    request = ResearchRequest(
        company_id=cid,
        question="Разберите фондирование и ликвидность по найденным источникам",
        seed_news_ids=[i["news_id"] for i in items],
        public_search=False,
    )
    job = service.research.create(request, "search-to-agent")
    found = {s["source_id"]: s for s in job["sources"]}
    for item in items:
        assert found[item["news_id"]]["text"] == item["text"]
        assert found[item["news_id"]]["provided_for_current_question"]
    assert service.research.create(request, "search-to-agent")["run_id"] == job["run_id"]
    with pytest.raises(NotFound):
        service.research.create(
            request.model_copy(update={"seed_news_ids": ["news_" + "fa" * 12]}), "foreign-news"
        )
    with pytest.raises(ValueError):
        service.research.create(request.model_copy(update={"seed_news_ids": ["bad-id"]}), "invalid-news")

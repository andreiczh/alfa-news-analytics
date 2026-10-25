import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from alfa_agent.api import create_app
from alfa_agent.bank_library import BankLibrary
from alfa_agent.identity import Principal, current_principal
from alfa_agent.public_sources import PublicSourceError, PageText, public_target, search_headlines
from alfa_agent.research import ResearchRequest, ResearchEngine, ResearchDraft, validate_draft
from alfa_agent.storage import Conflict, NotFound


NEWS = "Синтетическое сообщение для теста: поставщик сообщил об изменении условий поставок упаковки. Фактическая новость не подтверждена."


def draft_for(source_id="input_news", quote=NEWS):
    return {
        "title": "Проверить договорные условия поставок",
        "summary": "Сообщение пользователя требует сверки с договорами компании; влияние на затраты пока не установлено.",
        "findings": [
            {
                "finding_id": "f1",
                "kind": "hypothesis",
                "statement_type": "conditional",
                "text": "Если сообщение подтвердится, условия поставок могут потребовать пересмотра планирования.",
                "why_company": "Относится к компании только при наличии закупок у этого поставщика.",
                "consequence": "Возможна необходимость проверить сроки и стоимость закупок.",
                "condition": "Компания действительно закупает упаковку у указанного поставщика.",
                "priority": "check_now",
                "citations": [{"source_id": source_id, "quote": quote}],
            }
        ],
        "role_views": [
            {
                "role": "cfo",
                "focus": "Планирование затрат",
                "interpretation": "Без договора нельзя установить знак и размер эффекта.",
                "actions": ["Проверить актуальные условия договора."],
                "finding_ids": ["f1"],
            }
        ],
        "missing_data": ["Договор с поставщиком и доля этих закупок."],
        "limitations": ["Сообщение синтетическое, первоисточник не подтверждён."],
        "follow_up_questions": ["Какие условия договора потребуют проверки в первую очередь?"],
    }


class ResearchGateway:
    """Deterministic protocol test double. It is never shipped as a model response."""

    mode = "test_double"

    def __init__(self, bad_number=False, reject_once=False, ask=False, approved_warning=False):
        self.stages = []
        self.bad_number, self.reject_once, self.ask = bad_number, reject_once, ask
        self.payloads = []
        self.approved_warning = approved_warning

    async def complete(self, run_id, stage, payload, schema, repair=None):
        self.stages.append(stage)
        self.payloads.append(payload)
        if stage == "research_plan":
            tool = "ask_user" if self.ask else "write"
            self.ask = False
            return json.dumps(
                {
                    "rationale": "Проверить известные основания и сохранить ограничения.",
                    "actions": [
                        {
                            "tool": tool,
                            "reason": "Доступных источников достаточно для условного вывода.",
                            "questions": ["Есть ли закупки у этого поставщика?"]
                            if tool == "ask_user"
                            else [],
                        }
                    ],
                }
            )
        if stage == "research_write":
            bank = payload["company"]["declared_bank"]
            anchor = (
                next(a for a in payload["evidence_ledger"] if a["kind"] == "bank_claim")
                if bank
                else next(a for a in payload["evidence_ledger"] if a["source_id"] == "input_news")
            )
            value = {
                "findings": [
                    {
                        "mechanism": "bank_alm" if bank else "costs",
                        "anchor_ids": [anchor["anchor_id"]],
                        "priority": "check_now",
                    }
                ],
                "bank_anchor_id": anchor["anchor_id"] if bank else "",
            }
            if self.bad_number:
                value["findings"][0]["anchor_ids"] = ["a777"]
                self.bad_number = False
            return json.dumps(value, ensure_ascii=False)
        if stage == "research_narrative":
            ledger = payload["evidence_ledger"]
            anchor = next(
                (a for a in ledger if a["source_id"] == payload.get("selected_news_source_id")),
                next((a for a in ledger if a["source_id"] == "input_news"), ledger[0]),
            )
            fields = payload["company"].get("confirmed_context", {}).get("fields", {})
            context_anchor = next(
                (a for a in ledger if any("_context_" + key + "_" in a["source_id"] for key in fields)), None
            )
            refs = list(
                dict.fromkeys(
                    [anchor["anchor_id"], *([context_anchor["anchor_id"]] if context_anchor else [])]
                )
            )
            point = {
                "kind": "interpretation",
                "text": "Изменение условий требует проверки договоров и потребностей компании.",
                "anchor_ids": refs,
                "condition": "При наличии соответствующей экспозиции и сохранении условий источника.",
            }
            return json.dumps(
                {
                    "title": "Проверить условия и применимость события к компании",
                    "main_conclusion": point,
                    "situation": [
                        {
                            "message_id": next(
                                (
                                    o["message_id"]
                                    for o in payload["source_messages"]
                                    if o["anchor_id"] == anchor["anchor_id"]
                                ),
                                payload["source_messages"][0]["message_id"],
                            ),
                            "condition": point["condition"],
                        }
                    ],
                    "drivers": [point],
                    "company_impact": [point],
                    "risks": [point],
                    "opportunities": [],
                    "recommendations": [point],
                    "forecast_status": "insufficient_evidence",
                    "forecast_limit": "Недостаточно данных для прогноза направления и финансового эффекта.",
                    "scenarios": [],
                    "context_keys": list(fields)[:1],
                },
                ensure_ascii=False,
            )
        if stage == "research_verify":
            approve = not self.reject_once
            self.reject_once = False
            issues = (
                ["OCR источников требует ручной проверки; ограничение уже обозначено."]
                if self.approved_warning
                else []
            )
            return json.dumps(
                {
                    "approved": approve,
                    "issues": issues if approve else ["Нужно уточнить условный характер вывода."],
                    "scope_note": "Проверены источники и условный смысл, истинность новости не подтверждена.",
                }
            )
        raise AssertionError(stage)


def request_for(service, **kw):
    cid = service.workspace.create_company("Синтетическая компания")["company_id"]
    return ResearchRequest(
        company_id=cid,
        question="Как изменение условий поставок может повлиять на нашу компанию?",
        news_text=NEWS,
        public_search=False,
        **kw,
    )


def test_unseen_non_rate_news_runs_with_evidence_and_frozen_profile(service):
    request = request_for(service)
    service.workspace.save_context(request.company_id, "Первоначальное описание компании.", "")
    job = service.research.create(request, "new-news")
    service.workspace.save_context(request.company_id, "Изменённый контекст после запуска.", "")
    gateway = ResearchGateway()
    asyncio.run(ResearchEngine(service.research, gateway).once())
    result = service.research.get(job["run_id"])
    assert result["status"] == "PARTIAL" and result["verification"]["passed"]
    assert result["tool_calls"] == 2 and result["llm_calls"] == 4
    assert result["result"]["findings"][0]["citations"][0]["quote"] == NEWS
    assert "Первоначальное описание" in json.dumps(gateway.payloads, ensure_ascii=False)
    assert "Изменённый контекст" not in json.dumps(gateway.payloads, ensure_ascii=False)
    assert service.research.create(request, "new-news")["run_id"] == job["run_id"]


def test_approved_partial_research_retains_nonblocking_warning_without_replan(service):
    job = service.research.create(request_for(service), "approved-with-warning")
    asyncio.run(ResearchEngine(service.research, ResearchGateway(approved_warning=True)).once())
    result = service.research.get(job["run_id"])
    assert result["status"] == "PARTIAL" and result["replans"] == 0
    assert result["verification"]["warnings"]
    assert any("OCR источников" in x for x in result["result"]["limitations"])


@pytest.mark.parametrize("kw", [{"bad_number": True}, {"reject_once": True}])
def test_replans_after_invalid_anchor_or_semantic_failure_and_publishes_only_verified(service, kw):
    job = service.research.create(request_for(service), "replan")
    gateway = ResearchGateway(**kw)
    asyncio.run(ResearchEngine(service.research, gateway).once())
    result = service.research.get(job["run_id"])
    assert result["status"] == "PARTIAL" and result["replans"] == 1
    assert all(c["anchor_id"] != "a777" for f in result["result"]["findings"] for c in f["citations"])
    assert any(t["event"] == "replan" for t in result["trace"])
    assert sum(t["event"] == "published" for t in result["trace"]) == 1


def test_quote_source_owner_and_modality_are_enforced():
    source = {"source_id": "input_news", "kind": "user_news", "text": NEWS}
    value = ResearchDraft.model_validate(draft_for())
    assert not validate_draft(value, [source])
    value.findings[0].citations[0].quote = "Несуществующая цитата с вымышленной информацией."
    assert any("quote_not_in_source" in x for x in validate_draft(value, [source]))
    value = ResearchDraft.model_validate(draft_for())
    value.findings[0].kind = "bank_view"
    assert "bank_owner_not_supported" in validate_draft(value, [source])
    value.findings[0].kind = "source_fact"
    value.findings[0].statement_type = "forecast"
    assert any("forecast_not_a_fact" in x for x in validate_draft(value, [source]))
    value = ResearchDraft.model_validate(draft_for())
    assert any(
        "unread_or_unknown_source" in x for x in validate_draft(value, [{**source, "kind": "search_lead"}])
    )


def test_research_requires_same_user_and_company_for_follow_up(service):
    a = Principal("user_test_a", "company_a", "Тестовый Пользователь", "Компания", "CFO", "cfo")
    b = Principal("user_test_b", "company_b", "Другой Пользователь", "Компания", "CFO", "cfo")
    token = current_principal.set(a)
    try:
        request = request_for(service)
        job = service.research.create(request, "owned-research")
        other = service.workspace.create_company("Другая компания")["company_id"]
        with pytest.raises(Conflict, match="follow_up_company_mismatch"):
            service.research.create(
                request.model_copy(update={"company_id": other, "parent_id": job["run_id"]}), "wrong-parent"
            )
    finally:
        current_principal.reset(token)
    token = current_principal.set(b)
    try:
        with pytest.raises(NotFound):
            service.research.get(job["run_id"])
        with pytest.raises(NotFound):
            service.research.set_favorite(job["run_id"], True)
    finally:
        current_principal.reset(token)


def test_partial_mode_does_not_block_a_grounded_qualitative_answer_on_missing_balance(service):
    job = service.research.create(request_for(service), "partial-on-unknown")
    asyncio.run(ResearchEngine(service.research, ResearchGateway(ask=True)).once())
    result = service.research.get(job["run_id"])
    assert result["status"] == "PARTIAL" and result["verification"]["passed"]
    assert any(item["event"] == "partial_instead_of_missing_data_question" for item in result["trace"])


def test_ask_user_resume_and_cancel_are_real_states(service):
    job = service.research.create(
        request_for(service).model_copy(update={"allow_partial": False}), "question"
    )
    gateway = ResearchGateway(ask=True)
    engine = ResearchEngine(service.research, gateway)
    asyncio.run(engine.once())
    waiting = service.research.get(job["run_id"])
    assert waiting["status"] == "WAITING_INPUT"
    from alfa_agent.research import ResearchAnswer

    resumed = service.research.answer(
        job["run_id"],
        ResearchAnswer(
            answer="Закупки у поставщика есть, данные синтетические.", expected_version=waiting["version"]
        ),
    )
    assert resumed["status"] == "QUEUED"
    asyncio.run(engine.once())
    assert service.research.get(job["run_id"])["verification"]["passed"]
    second = service.research.create(request_for(service), "cancelled")
    service.research.cancel(second["run_id"])
    assert asyncio.run(engine.once()) is None
    assert service.research.get(second["run_id"])["status"] == "CANCELLED"


def test_follow_up_keeps_full_parent_evidence_and_prioritizes_new_news(service):
    request = request_for(service)
    parent = service.research.create(request, "parent")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    row = service.research._row(parent["run_id"])
    for i in range(20):
        row["state"]["sources"].append(
            {
                "source_id": f"old_source_{i}",
                "kind": "public_page",
                "title": "Explicit synthetic fixture",
                "text": "Синтетический старый материал для теста сохранности источников.",
            }
        )
    with service.store.connection() as db:
        db.execute(
            "UPDATE research_jobs SET state_json=? WHERE id=?", (json.dumps(row["state"]), parent["run_id"])
        )
    fresh = "Синтетическая новая новость: поставщик меняет тарифы на упаковку. Это тест, не подтверждённое рыночное событие."
    child = service.research.create(
        request.model_copy(
            update={
                "parent_id": parent["run_id"],
                "question": "Как новая новость о тарифах меняет наш сценарий?",
                "news_text": fresh,
            }
        ),
        "child",
    )
    assert {s["source_id"] for s in row["state"]["sources"]} <= {s["source_id"] for s in child["sources"]}
    child_row = service.research._row(child["run_id"])
    assert child_row["input"]["snapshot_id"] == row["input"]["snapshot_id"]
    from alfa_agent.research import evidence_ledger

    ledger = evidence_ledger(child["sources"], child["question"])
    assert any(a["quote"] == fresh for a in ledger[:4])


def test_bank_corpus_has_all_nine_documents_and_preserves_unknown_date():
    bank = BankLibrary(Path(__file__).parents[1])
    assert len(bank.documents) == 9 and sum(d["page_count"] for d in bank.documents) == 64
    assert next(d for d in bank.documents if "Gold" in d["filename"])["published_at"] is None
    assert next(d for d in bank.documents if "Steels" in d["filename"])["published_at"] == "2021-04-15"
    matches = bank.search("банки фондирование кредит ликвидность ключевая ставка")
    assert matches and all(m["page"] >= 1 and m["review_status"] == "ocr_pending_review" for m in matches)
    assert all(
        m["text"]
        in next(
            p["text"]
            for d in bank.documents
            if d["document_id"] == m["document_id"]
            for p in d["pages"]
            if p["page"] == m["page"]
        )
        for m in matches
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://127.0.0.1",
        "https://[::1]",
        "https://localhost",
        "https://example.com:8443",
        "https://metadata.google.internal",
    ],
)
def test_private_targets_and_credentials_are_blocked(url):
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with pytest.raises(PublicSourceError):
            public_target(url)


def test_search_uses_only_public_name_and_server_topic_not_private_question():
    captured = []

    def read(url):
        captured.append(url)
        return {
            "text": "<rss><channel><item><title>Headline</title><link>https://example.com/news</link></item></channel></rss>"
        }

    with patch("alfa_agent.public_sources.read_public", side_effect=read):
        result = search_headlines("Публичная компания", "rates")
    assert len(captured) == 1 and "Публичная" not in captured[0]
    assert result[0]["kind"] == "search_lead"
    with pytest.raises(PublicSourceError):
        search_headlines("Публичная компания", "Внутренний долг 999999")


def test_html_extraction_ignores_scripts_and_prioritizes_article():
    parser = PageText()
    parser.feed(
        "<title>Title</title><nav>Navigation</nav><main><p>Source content.</p><script>secret()</script></main>"
    )
    assert "Source content." in "".join(parser.main_parts)
    assert "secret" not in "".join(parser.parts)


def test_research_api_owned_history_favorite_feedback_and_export(service):
    with TestClient(create_app(service.settings)) as client:
        token = client.app.state.token
        client.headers["x-app-token"] = token
        cid = client.post("/api/companies", json={"name": "Синтетическая компания API"}).json()["company_id"]
        job = client.post(
            "/api/research",
            headers={"Idempotency-Key": "api-research"},
            json={
                "company_id": cid,
                "question": "Какие условия поставок следует проверить?",
                "news_text": NEWS,
                "public_search": False,
            },
        ).json()
        assert job["status"] == "QUEUED"
        assert client.get("/api/research/" + job["run_id"] + "/export").status_code == 409
        asyncio.run(ResearchEngine(client.app.state.service.research, ResearchGateway()).once())
        assert client.get("/api/research/" + job["run_id"]).json()["verification"]["passed"]
        assert (
            client.put("/api/research/" + job["run_id"] + "/favorite", json={"saved": True}).status_code
            == 200
        )
        assert client.get("/api/research", params={"company_id": cid}).json()[0]["favorite"]
        assert (
            client.post("/api/research/" + job["run_id"] + "/feedback", json={"rating": "useful"}).status_code
            == 201
        )
        assert client.get("/api/research/" + job["run_id"] + "/export").status_code == 200


def test_overview_memo_cache_respects_role_and_profile_versions(service):
    request = request_for(service)
    job = service.research.create(request, "memo-cache")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    result = service.research.get(job["run_id"])
    assert result["result"]["narrative"]
    version = result["result"]["profile_version"]
    assert (
        service.research.latest_decision_memo(request.company_id, "cfo", version, 0)["run_id"]
        == job["run_id"]
    )
    assert service.research.latest_decision_memo(request.company_id, "cmo", version, 0) is None
    assert service.research.latest_decision_memo(request.company_id, "cfo", version + 1, 0) is None
    assert service.research.latest_decision_memo(request.company_id, "cfo", version, 1) is None


def test_selected_bank_material_does_not_pull_structured_claims_from_another_pdf(service):
    service.research.bank = BankLibrary(Path(__file__).parents[1])
    request = request_for(service).model_copy(
        update={
            "bank_document_id": "bank_case_09",
            "question": "Как условия ставок могут повлиять на компанию по выбранному банковскому материалу?",
        }
    )
    job = service.research.create(request, "selected-bank-material")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    sources = service.research.get(job["run_id"])["sources"]
    excerpts = [s for s in sources if s["kind"] == "bank_excerpt"]
    assert excerpts and {s["document_id"] for s in excerpts} == {"bank_case_09"}
    assert not any(s["kind"] == "bank_claim" for s in sources)


def test_explicit_global_role_is_not_reinferred_from_custom_job_title(service):
    request = request_for(service).model_copy(
        update={"role": "other", "job_title": "Финансовый директор по маркетингу"}
    )
    job = service.research.create(request, "global-role-authoritative")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    result = service.research.get(job["run_id"])
    assert result["result"]["narrative"]["role"] == "other"


def test_semantic_verifier_keeps_exact_evidence_without_duplicate_article_context():
    from alfa_agent.research import semantic_verification_input

    draft = ResearchDraft.model_validate(draft_for())
    draft.narrative = {
        "evidence": [
            {
                "anchor_id": "a1",
                "source_id": "input_news",
                "quote": NEWS,
                "published_at": "2026-10-23",
                "publisher": "Тестовый источник",
                "context_key": None,
                "page": 3,
            }
        ]
    }
    original = draft.model_dump(mode="json")
    sources = [
        {
            "source_id": "input_news",
            "kind": "user_news",
            "text": NEWS + " Сохранённый исходный текст." * 5000,
            "publisher": "Тестовый источник",
            "published_at": "2026-10-23",
            "review_status": "user_supplied",
        },
        {"source_id": "unused", "kind": "search_lead", "text": "Нерелевантный текст" * 5000},
    ]
    payload = semantic_verification_input(draft, sources)
    assert len(json.dumps(payload, ensure_ascii=False)) < 10000
    assert payload["draft"]["findings"][0]["citations"][0]["quote"] == NEWS
    assert payload["draft"]["narrative"]["evidence"][0]["quote"] == NEWS
    assert payload["draft"]["narrative"]["evidence"][0]["page"] == 3
    assert len(payload["sources"]) == 1 and payload["sources"][0]["published_at"] == "2026-10-23"
    assert payload["sources"][0]["publisher"] == "Тестовый источник"
    assert draft.model_dump(mode="json") == original


def test_draft_header_uses_same_calendar_normalization_as_narrative():
    draft = ResearchDraft.model_validate(draft_for())
    quote = "Решение принято 23 октября 2026 года."
    draft.title = "Решение 23.10 требует проверки применимости"
    draft.narrative = {"evidence": [{"source_id": "calendar", "quote": quote, "published_at": "2026-10-23"}]}
    sources = [
        {"source_id": "input_news", "kind": "user_news", "text": NEWS},
        {"source_id": "calendar", "kind": "public_page", "text": quote},
    ]
    assert validate_draft(draft, sources) == []
    draft.title = "Решение требует проверки эффекта в размере 777 рублей"
    assert "unsupported_summary_number" in validate_draft(draft, sources)

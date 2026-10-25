"""Meaningful boundaries of global preferences, reviewed context and grounded scenarios."""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from alfa_agent.api import create_app
from alfa_agent.company_dialogue import ContextConfirm, ContextEngine, ContextMessage
from alfa_agent.identity import create_principal, current_principal, role_for
from alfa_agent.narrative import DecisionNarrative, validate_narrative
from alfa_agent.news_ranking import company_context, rank_news
from alfa_agent.personalization import PreferencesUpdate
from alfa_agent.settings import utcnow
from alfa_agent.storage import Conflict


class ContextGateway:
    mode = "test_double"

    def __init__(self, invented=False):
        self.invented, self.calls = invented, []

    async def complete(self, run_id, stage, payload, schema, repair=None, **kw):
        self.calls.append((stage, payload))
        message = next(m for m in reversed(payload["messages"]) if m["role"] == "user")
        return json.dumps(
            {
                "response": "Предлагаю сохранить ваш приоритет после просмотра и подтверждения.",
                "fields": [
                    {
                        "key": "priorities",
                        "category": "priorities",
                        "label": "Что отслеживать",
                        "value": "Выручка составила 999 млрд рублей." if self.invented else message["text"],
                        "message_id": message["id"],
                        "quote": message["text"],
                    }
                ],
                "questions": ["На каком горизонте этот приоритет особенно важен?"],
            },
            ensure_ascii=False,
        )


def field(value="Спрос и продажи в наших целевых сегментах"):
    return {"key": "priorities", "category": "priorities", "label": "Что отслеживать", "value": value}


def own_company(service):
    return service.workspace.create_company("Тестовая компания контекста")["company_id"]


def test_dialogue_drafts_then_user_confirms_frozen_context(service):
    cid = own_company(service)
    before = service.workspace.snapshot(cid, service.corpus, persist=True)
    turn = service.context_dialogue.create(
        cid, ContextMessage(message=field()["value"], expected_revision=0), "dialogue-one"
    )
    gateway = ContextGateway()
    asyncio.run(ContextEngine(service.context_dialogue, gateway).once())
    assert service.workspace.context(cid)["fields"] == {}
    proposed = service.context_dialogue.history(cid)["turns"][-1]
    assert proposed["status"] == "SUCCEEDED"
    assert len(gateway.calls) == 1
    payload = ContextConfirm(expected_revision=0, fields=[field()])
    result = service.context_dialogue.confirm(cid, turn["turn_id"], payload)
    assert result["revision"] == 1 and result["fields"]["priorities"]["origin"] == "user_reviewed"
    assert service.context_dialogue.confirm(cid, turn["turn_id"], payload) == result
    frozen = service.workspace.load_snapshot(before["snapshot_id"], cid)
    assert frozen["context"]["fields"] == {}
    current = service.workspace.snapshot(cid, service.corpus)
    assert current["profile"]["version"] > before["profile"]["version"]
    assert any(f["name"] == "information_priorities" for f in current["profile"]["features"])
    assert any(field()["value"] in e["quote"] for e in current["evidence"])


def test_model_cannot_invent_company_values(service):
    cid = own_company(service)
    service.context_dialogue.create(
        cid, ContextMessage(message="Следим за спросом клиентов.", expected_revision=0), "dialogue-bad"
    )
    gateway = ContextGateway(invented=True)
    asyncio.run(ContextEngine(service.context_dialogue, gateway).once())
    turn = service.context_dialogue.history(cid)["turns"][-1]
    assert turn["status"] == "FAILED" and turn["error_code"] == "context_contract_invalid"
    assert len(gateway.calls) == 2
    assert service.workspace.context(cid)["fields"] == {}


def test_stale_proposal_does_not_overwrite_new_context(service):
    cid = own_company(service)
    turn = service.context_dialogue.create(
        cid, ContextMessage(message="Следим за спросом клиентов.", expected_revision=0), "dialogue-stale"
    )
    asyncio.run(ContextEngine(service.context_dialogue, ContextGateway()).once())
    service.context_dialogue.save_fields(
        cid, ContextConfirm(expected_revision=0, fields=[field("Инфляция и затраты")])
    )
    with pytest.raises(Conflict, match="company_context_changed"):
        service.context_dialogue.confirm(
            cid, turn["turn_id"], ContextConfirm(expected_revision=0, fields=[field()])
        )
    assert service.workspace.context(cid)["fields"]["priorities"]["value"] == "Инфляция и затраты"


def test_context_interruption_requires_manual_new_turn(service):
    cid = own_company(service)
    turn = service.context_dialogue.create(
        cid, ContextMessage(message="Следим за спросом клиентов.", expected_revision=0), "dialogue-stop"
    )
    with service.store.connection() as db:
        db.execute(
            "UPDATE context_turns SET status='RUNNING',lease_until=? WHERE id=?",
            (utcnow().timestamp() - 1, turn["turn_id"]),
        )
    gateway = ContextGateway()
    assert asyncio.run(ContextEngine(service.context_dialogue, gateway).once()) is None
    assert gateway.calls == []
    assert (
        service.context_dialogue.history(cid)["turns"][-1]["error_code"] == "context_interrupted_manual_retry"
    )


def test_preferences_persist_despite_original_session_role_and_isolate_people(service, monkeypatch):
    monkeypatch.setenv("ALFA_ACCESS_PASSWORD", "synthetic-team-password-for-tests")
    with (
        TestClient(create_app(service.settings)) as first,
        TestClient(create_app(service.settings)) as second,
    ):
        form = {
            "full_name": "Тестовый Пользователь",
            "company_name": "Синтетическая компания",
            "job_title": "CFO",
            "password": "synthetic-team-password-for-tests",
        }
        first.post("/auth/login", data=form)
        first.get("/")
        import re

        token = re.search(r'name="app-token" content="([^"]+)"', first.get("/").text)[1]
        headers = {"X-App-Token": token, "Origin": "http://testserver"}
        original = first.get("/api/me", headers=headers).json()
        cid = original["company_id"]
        response = first.put("/api/me/preferences", json={"company_id": cid, "role": "cmo"}, headers=headers)
        assert response.status_code == 200
        assert first.get("/api/me", headers=headers).json()["role"] == "cmo"
        assert first.get("/api/me", headers=headers).json()["job_title"] == "Директор по маркетингу"
        second.post("/auth/login", data=form)
        token2 = re.search(r'name="app-token" content="([^"]+)"', second.get("/").text)[1]
        other_headers = {"X-App-Token": token2, "Origin": "http://testserver"}
        assert second.get("/api/me", headers=other_headers).json()["role"] == "cfo"
        assert (
            second.put(
                "/api/me/preferences", json={"company_id": cid, "role": "ceo"}, headers=other_headers
            ).status_code
            == 403
        )
        assert second.get(f"/api/companies/{cid}/context/dialogue", headers=other_headers).status_code == 403
        assert first.get(f"/api/companies/{cid}/news", headers=headers).json()["role"] == "cmo"
        assert first.get(f"/api/library?company_id={cid}", headers=headers).json()["role"] == "cmo"


def test_interview_uses_saved_marketing_role(service):
    principal = create_principal(service.workspace, "Тестовый Маркетолог", "Тестовая компания", "CFO")
    token = current_principal.set(principal)
    try:
        service.personalization.save(PreferencesUpdate(company_id=principal.company_id, role="cmo"))
        service.context_dialogue.create(
            principal.company_id,
            ContextMessage(message="Важен спрос клиентов.", expected_revision=0),
            "context-role",
        )
        gateway = ContextGateway()
        asyncio.run(ContextEngine(service.context_dialogue, gateway).once())
        assert gateway.calls[0][1]["role"] == "cmo"
        assert role_for("Директор по маркетингу") == "cmo"
    finally:
        current_principal.reset(token)


def test_confirmed_priorities_really_change_news_selection(service):
    cid = own_company(service)
    item = {
        "news_id": "news_" + "a" * 24,
        "title": "Изменение спроса и продаж на рынке",
        "excerpt": "",
        "url": "https://example.org/news",
        "publisher": "Тестовый источник",
        "published_at": (utcnow() - timedelta(hours=1)).isoformat(),
        "read_status": "article_read",
    }
    before = company_context(service.workspace.snapshot(cid, service.corpus))
    assert rank_news([item], before, "cmo", utcnow())[0] == []
    service.context_dialogue.save_fields(cid, ContextConfirm(expected_revision=0, fields=[field()]))
    after = company_context(service.workspace.snapshot(cid, service.corpus))
    ranked = rank_news([item], after, "cmo", utcnow())[0]
    assert len(ranked) == 1
    assert ranked[0]["matched_priorities"] == ["Что отслеживать"]
    assert "маркет" in ranked[0]["decision"]["role"].lower()
    assert ranked[0]["decision"]["materiality"] == "Размер влияния не установлен"


def narrative(anchor="a1"):
    p = {
        "kind": "interpretation",
        "text": "Если условия изменятся, нужно проверить планирование затрат компании.",
        "condition": "При наличии договоров, зависящих от описанного фактора.",
        "anchor_ids": [anchor],
    }
    return DecisionNarrative.model_validate(
        {
            "title": "Проверить чувствительность затрат компании",
            "main_conclusion": p,
            "situation": [p],
            "drivers": [p],
            "company_impact": [p],
            "risks": [],
            "opportunities": [],
            "recommendations": [p],
            "forecast_status": "insufficient_evidence",
            "scenarios": [],
            "context_keys": [],
            "forecast_limit": "Для прогноза нужны внутренние условия договоров и уточнение механизма.",
        }
    )


def test_narrative_rejects_unknown_anchors_numbers_and_unconditional_interpretation():
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "s1",
            "kind": "public_page",
            "quote": "Изменились условия поставок.",
            "published_at": None,
        }
    ]
    assert validate_narrative(narrative(), ledger, {}) == []
    assert "unknown_narrative_anchor" in validate_narrative(narrative("a404"), ledger, {})
    value = narrative()
    value.main_conclusion.text = "Прибыль компании вырастет на 71 процент."
    value.main_conclusion.condition = ""
    issues = validate_narrative(value, ledger, {})
    assert "unsupported_narrative_number:0" in issues and "narrative_interpretation_needs_condition" in issues


def test_forecasts_require_alternative_evidence_and_no_invented_probability():
    value = narrative()
    value.forecast_status = "conditional_scenarios"
    value.scenarios = []
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "s1",
            "kind": "public_page",
            "quote": "Изменились условия поставок.",
            "published_at": None,
        }
    ]
    assert "forecast_needs_base_and_alternative" in validate_narrative(value, ledger, {})


def test_scenario_contract_accepts_grounded_alternatives_and_blocks_probability():
    from alfa_agent.narrative import ForecastScenario

    value = narrative()
    value.forecast_status = "conditional_scenarios"
    base = {
        "variant": "base",
        "title": "При сохранении условий",
        "outcome": "При сохранении условий возможна переоценка договорных затрат.",
        "horizon": "Ближайший квартал как плановый горизонт",
        "mechanism": "Условия поставок передаются через закупочные договоры в затраты компании.",
        "assumptions": ["Договоры компании зависят от этих условий."],
        "change_signals": ["Пересмотр условий поставщиком."],
        "anchor_ids": ["a1"],
        "confidence_reason": "Есть сообщение источника; внутренние условия договоров не предоставлены.",
    }
    value.scenarios = [
        ForecastScenario.model_validate(base),
        ForecastScenario.model_validate(
            {
                **base,
                "variant": "alternative",
                "title": "При пересмотре условий",
                "outcome": "При пересмотре условий канал влияния может измениться.",
            }
        ),
    ]
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "s1",
            "kind": "public_page",
            "quote": "Изменились условия поставок.",
            "published_at": None,
        }
    ]
    assert validate_narrative(value, ledger, {}) == []
    value.scenarios[0].confidence_reason = "Вероятность сценария составляет 97 процентов."
    issues = validate_narrative(value, ledger, {})
    assert "unsupported_forecast_number" in issues and "unjustified_forecast_probability" in issues


def test_context_reference_requires_actual_context_evidence():
    value = narrative()
    value.context_keys = ["priorities"]
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "external_source",
            "kind": "public_page",
            "quote": "Изменились условия поставок.",
            "published_at": None,
        }
    ]
    assert "confirmed_context_has_no_grounding" in validate_narrative(
        value, ledger, {"fields": {"priorities": field()}}
    )


def test_competitor_from_natural_list_is_not_own_company_exposure(service):
    cid = own_company(service)
    service.context_dialogue.save_fields(
        cid,
        ContextConfirm(
            expected_revision=0,
            fields=[
                {
                    "key": "tracked_competitors",
                    "category": "priorities",
                    "label": "Наблюдать конкурентов",
                    "value": "Конкуренты для наблюдения: Сбербанк, ВТБ.",
                }
            ],
        ),
    )
    context = company_context(service.workspace.snapshot(cid, service.corpus))
    assert context["competitors"] == ["Сбербанк", "ВТБ"]
    item = {
        "news_id": "news_" + "a" * 24,
        "title": "Сбербанк запустил продукт для клиентов",
        "excerpt": "",
        "url": "https://example.org/news",
        "publisher": "Тестовый источник",
        "published_at": utcnow().isoformat(),
        "read_status": "article_read",
    }
    ranked = rank_news([item], context, "cmo", utcnow())[0]
    assert ranked[0]["company_relation"] == "competitor"
    assert "Макроэкономический фактор" not in " ".join(ranked[0]["reasons"])
    assert ranked[0]["decision"]["urgent"] is False


def test_bank_library_changes_with_role_without_forcing_specialized_relevance(service):
    from pathlib import Path
    from alfa_agent.bank_library import BankLibrary

    library = BankLibrary(Path(__file__).parents[1])
    cid = own_company(service)
    snapshot = service.workspace.snapshot(cid, service.corpus)
    cfo = {d["document_id"]: d for d in library.recommend(snapshot, "cfo")}
    cmo = {d["document_id"]: d for d in library.recommend(snapshot, "cmo")}
    assert cmo["bank_case_01"]["relevance"]["recommended"] is True
    assert cfo["bank_case_01"]["relevance"]["recommended"] is False
    assert cmo["bank_case_08"]["relevance"]["recommended"] is False
    assert cmo["bank_case_02"]["relevance"]["recommended"] is False


def test_large_free_context_is_chunked_without_truncating_saved_material(service):
    cid = own_company(service)
    text = "Сведения пользователя о целях и ограничениях. " * 90
    service.workspace.save_context(cid, text, "")
    snapshot = service.workspace.snapshot(cid, service.corpus)
    assert text.strip() in next(
        f["text_value"] for f in snapshot["profile"]["features"] if f["name"] == "company_description"
    )
    assert all(len(e["quote"]) <= 2500 for e in snapshot["evidence"])
    assert text.strip() in snapshot["document_text"][cid + "_onboarding"]


def test_narrative_cannot_swap_currency_scale_or_duration_for_same_number():
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "s1",
            "kind": "company_evidence",
            "quote": "Планируемые расходы: 10 млн рублей. Изменение ставки: 14 процентов.",
            "published_at": None,
        }
    ]
    value = narrative()
    value.main_conclusion.text = "В источнике указаны расходы 10 млн рублей."
    assert validate_narrative(value, ledger, {}) == []
    for text in (
        "В источнике указаны расходы 10 млн долларов.",
        "В источнике указаны расходы 10 млрд рублей.",
        "Условия могут измениться через 14 дней.",
    ):
        value.main_conclusion.text = text
        assert "unsupported_narrative_unit:0" in validate_narrative(value, ledger, {})


def test_qualitative_probability_reference_to_historical_year_is_not_percentage():
    from alfa_agent.narrative import probability_claim

    assert not probability_claim(
        "Автор указывает низкую вероятность повторения 2024 года, когда дорожало фондирование."
    )
    assert not probability_claim(
        "Численная вероятность не оценена; ключевая ставка в источнике — 14 процентов."
    )
    assert probability_claim("Вероятность сценария составляет 97 процентов.")
    assert probability_claim("Вероятность сценария равна 0,9.")


def test_spelled_out_measured_values_also_need_their_own_evidence():
    value = narrative()
    ledger = [
        {
            "anchor_id": "a1",
            "source_id": "s1",
            "kind": "public_page",
            "quote": "Ключевая ставка сохранена на уровне 14,0%.",
            "published_at": None,
        }
    ]
    value.main_conclusion.text = "В источнике ставка сохранена на уровне четырнадцати процентов."
    assert validate_narrative(value, ledger, {}) == []
    value.main_conclusion.text = "В источнике ставка сохранена на уровне двадцати пяти процентов."
    assert "unsupported_narrative_number:0" in validate_narrative(value, ledger, {})


def test_test_marker_survives_context_structuring_but_public_info_is_not_synthetic(service):
    from alfa_agent.personalization import marked_test_context

    assert not marked_test_context("Это открытые сведения, не внутренние данные банка.")
    assert not marked_test_context("Компания разрабатывает тестовый профиль для платформы.")
    assert marked_test_context("Тестовый профиль продукта, не данные клиента. Компания — банк.")
    cid = own_company(service)
    turn = service.context_dialogue.create(
        cid,
        ContextMessage(message="Тестовый профиль продукта. Следим за спросом клиентов.", expected_revision=0),
        "test-marker",
    )
    asyncio.run(ContextEngine(service.context_dialogue, ContextGateway()).once())
    service.context_dialogue.confirm(
        cid,
        turn["turn_id"],
        ContextConfirm(expected_revision=0, fields=[field("Следим за спросом клиентов.")]),
    )
    assert service.workspace.context(cid)["synthetic"] is True


def test_editing_legacy_reviewed_context_preserves_original_test_provenance(service):
    cid = own_company(service)
    message = "Тестовый профиль продукта. Приоритет: стоимость фондирования."
    turn = service.context_dialogue.create(
        cid, ContextMessage(message=message, expected_revision=0), "legacy-test-provenance"
    )
    asyncio.run(ContextEngine(service.context_dialogue, ContextGateway()).once())
    service.context_dialogue.confirm(
        cid, turn["turn_id"], ContextConfirm(expected_revision=0, fields=[field(message)])
    )
    with service.store.connection() as db:
        row = db.execute("SELECT fields_json FROM company_strategy WHERE company_id=?", (cid,)).fetchone()
        previous = json.loads(row["fields_json"])
        previous["priorities"].pop("synthetic")
        previous["priorities"]["value"] = "Стоимость фондирования"
        db.execute(
            "UPDATE company_strategy SET fields_json=? WHERE company_id=?", (json.dumps(previous), cid)
        )
    assert service.workspace.context(cid)["synthetic"] is True
    updated = service.context_dialogue.save_fields(
        cid, ContextConfirm(expected_revision=1, fields=[field("Стоимость фондирования и депозитные ставки")])
    )
    assert updated["revision"] == 2
    assert updated["synthetic"] is True
    assert updated["fields"]["priorities"]["synthetic"] is True
    assert updated["fields"]["priorities"]["origin"] == "user_edited"

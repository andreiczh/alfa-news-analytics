import asyncio

from alfa_agent.research import (
    ResearchDraft,
    ResearchRequest,
    ResearchEngine,
    evidence_ledger,
    bind_citations,
    validate_draft,
    numbers,
    ResearchSelection,
    assemble_research,
)
from test_research import ResearchGateway, draft_for, NEWS


def test_host_binds_quote_by_anchor_without_trusting_model_quote():
    sources = [{"source_id": "input_news", "kind": "user_news", "text": NEWS}]
    ledger = evidence_ledger(sources, "поставки")
    raw = draft_for()
    raw["findings"][0]["citations"] = [
        {"anchor_id": ledger[0]["anchor_id"], "quote": "Invented quote must be replaced by the source"}
    ]
    draft = ResearchDraft.model_validate(raw)
    bind_citations(draft, ledger)
    assert draft.findings[0].citations[0].quote == NEWS
    assert not validate_draft(draft, sources)
    draft.findings[0].citations[0].anchor_id = "unknown"
    draft.findings[0].citations[0].source_id = ""
    bind_citations(draft, ledger)
    assert validate_draft(draft, sources)


def test_bank_company_gets_attributed_bank_claims_without_corporate_debt_formula(service):
    cid = service.workspace.create_company("Синтетический банк")["company_id"]
    job = service.research.create(
        ResearchRequest(
            company_id=cid,
            question="Какие факторы влияют на ликвидность банка?",
            news_text=NEWS,
            public_search=False,
        ),
        "bank-ledger",
    )
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    result = service.research.get(job["run_id"])
    assert result["status"] == "PARTIAL"
    assert any(
        s["kind"] == "bank_claim" and s["claim_owner"].startswith("Альфа-Банк") for s in result["sources"]
    )


def test_numeric_grounding_preserves_sign_and_accepts_equivalent_dates_and_decimals():
    assert numbers("14,0%") == numbers("14%")
    assert numbers("11.09.2026") == numbers("2026-09-11")
    assert numbers("-1%") != numbers("+1%")


def test_cbr_event_never_displaces_dated_bank_view_and_duplicate_mechanisms_merge():
    sources = [
        {
            "source_id": "company_context",
            "kind": "company_evidence",
            "text": "Название, указанное пользователем: Синтетический банк.",
        },
        {
            "source_id": "cbr_page",
            "kind": "public_page",
            "text": "Банк России принял решение сохранить ключевую ставку на уровне 14 процентов.",
            "published_at": "2026-10-23",
        },
        {
            "source_id": "bank_assessment",
            "kind": "bank_claim",
            "text": "Ставка без изменения на 14,0%, риторика стала жёстче",
            "published_at": "2026-09-11",
            "claim_type": "ASSESSMENT",
        },
    ]
    ledger = evidence_ledger(sources, "Процентный результат банка")
    cbr = next(a["anchor_id"] for a in ledger if a["source_id"] == "cbr_page")
    choice = {"mechanism": "bank_alm", "anchor_ids": [cbr], "priority": "check_now"}
    selected = ResearchSelection(findings=[choice, choice], bank_anchor_id="")
    draft = assemble_research(selected, ledger, {"bank_company": True}, {"profile": {"features": []}})
    assert len([f for f in draft.findings if f.kind == "analysis"]) == 1
    bank = next(f for f in draft.findings if f.kind == "bank_view")
    assert "11.09.2026" in bank.text
    assert bank.citations[0].source_id == "bank_assessment"
    assert not validate_draft(draft, sources, bank_company=True)


def test_bank_supplier_tariff_research_uses_expense_budget_not_alm_actions():
    sources = [
        {
            "source_id": "supplier_news",
            "kind": "user_news",
            "text": "Синтетическая новая новость: поставщик меняет тариф на обслуживание ИТ. Применимость к банку не установлена.",
        }
    ]
    ledger = evidence_ledger(sources, "Как тариф влияет на затраты банка?")
    selection = ResearchSelection(
        findings=[{"mechanism": "costs", "anchor_ids": [ledger[0]["anchor_id"]], "priority": "check_now"}]
    )
    draft = assemble_research(selection, ledger, {"bank_company": True}, {"profile": {"features": []}})
    cfo = next(v for v in draft.role_views if v.role == "cfo")
    assert cfo.focus == "Бюджет и маржа"
    assert all("процентного результата" not in action for action in cfo.actions)
    assert not any(f.kind == "bank_view" for f in draft.findings)

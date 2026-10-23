import asyncio

from alfa_agent.research import (
    ResearchDraft,
    ResearchRequest,
    ResearchEngine,
    evidence_ledger,
    bind_citations,
    validate_draft,
    numbers,
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

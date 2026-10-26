import asyncio
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from alfa_agent.api import create_app
from alfa_agent.bank_library import BankLibrary
from alfa_agent.company_dialogue import ContextConfirm
from alfa_agent.funding import DebtTerms, funding_analysis, terms_json
from alfa_agent.research import ResearchEngine, ResearchRequest
from test_research import ResearchGateway, NEWS
from test_workspace_search import pool

ROOT = Path(__file__).parents[1]
ASOF = "2026-10-26T12:00:00+03:00"


def terms(variant="floating", **overrides):
    return DebtTerms(
        **{
            "rate_type": variant,
            "principal_rub": "200000000",
            "currency": "RUB",
            "benchmark": "key_rate",
            "repricing_at": "2026-11-25",
            "maturity_at": "2027-10-26",
            "fixed_until": "2027-10-26",
            "constant_principal_confirmed": True,
            "no_hedge_confirmed": True,
            "no_rate_review_confirmed": True,
            "synthetic": True,
            **overrides,
        }
    )


def snapshot(t):
    return {"context": {"fields": {"debt_contract": {"value": terms_json(t)}}}}


@pytest.mark.parametrize("role", ["cfo", "treasurer"])
@pytest.mark.parametrize("variant", ["floating", "fixed", "unknown"])
def test_three_debt_profiles_have_different_financial_behavior(role, variant):
    r = funding_analysis(snapshot(terms(variant)), role, ASOF, 90)
    assert r["net_company_effect"] == "not_calculated" and r["event_effect"] == "not_established"
    if variant == "floating":
        assert (
            r["scenarios"][2]["gross_interest_change_rub"] == "328767.12"
            and r["scenarios"][2]["exposure_days"] == 60
        )
    elif variant == "fixed":
        assert (
            all(Decimal(s["gross_interest_change_rub"]) == 0 for s in r["scenarios"])
            and r["protected_end"] == "2027-01-23"
        )
    else:
        assert not r["scenarios"] and r["missing_data"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal_rub": None},
        {"currency": "USD"},
        {"benchmark": "unknown"},
        {"repricing_at": None},
        {"maturity_at": None},
        {"constant_principal_confirmed": False},
        {"no_hedge_confirmed": False},
        {"repricing_at": "2028-01-01"},
    ],
)
def test_incomplete_or_unsupported_floating_terms_never_produce_a_number(overrides):
    r = funding_analysis(snapshot(terms(**overrides)), "cfo", ASOF, 90)
    assert not r["scenarios"] and r["missing_data"]


def test_maturity_partial_fixed_future_repricing_and_bank_guard():
    r = funding_analysis(snapshot(terms("fixed", maturity_at="2026-11-25")), "cfo", ASOF, 90)
    assert r["status"] == "fixed_partial_window" and r["protected_end"] == "2026-11-24" and r["missing_data"]
    r = funding_analysis(snapshot(terms(repricing_at="2027-02-01")), "treasurer", ASOF, 90)
    assert r["scenarios"][2]["exposure_days"] == 0 and r["effective_start"] is None
    r = funding_analysis(snapshot(terms(maturity_at="2026-11-26")), "cfo", ASOF, 90)
    assert r["scenarios"][2]["exposure_days"] == 1 and r["missing_data"]
    r = funding_analysis(
        snapshot(terms(repricing_at="2026-09-01", maturity_at="2026-10-20")), "cfo", ASOF, 90
    )
    assert r["scenarios"][2]["exposure_days"] == 0 and "погашен" in r["zero_reason"]
    assert r["effective_end"] is None and r["missing_data"]
    for variant in ["floating", "fixed"]:
        r = funding_analysis(snapshot(terms(variant)), "cfo", ASOF, 90, True)
        assert r["status"] == "bank_alm_required" and not r["scenarios"]
    assert not funding_analysis(snapshot(terms("fixed", currency="USD")), "cfo", ASOF, 90)["scenarios"]


def test_bad_numbers_rejected_and_stale_save_cannot_overwrite(service):
    for n in ["NaN", "Infinity", "0", "-1", "1000000000000001"]:
        with pytest.raises(ValueError):
            terms(principal_rub=n)
    with TestClient(create_app(service.settings)) as client:
        client.headers["x-app-token"] = client.app.state.token
        cid = client.post("/api/companies", json={"name": "Синтетический завод"}).json()["company_id"]
        data = {"expected_revision": 0, "terms": terms().model_dump(mode="json")}
        assert client.put(f"/api/companies/{cid}/funding", json=data).status_code == 200
        assert client.put(f"/api/companies/{cid}/funding", json=data).status_code == 409
        assert client.get(f"/api/companies/{cid}/funding").json()["terms"]["synthetic"]


def test_curated_bank_sources_are_verbatim_atomic_typed_and_not_human_approved():
    library = BankLibrary(ROOT)
    assert len(library.annotations) == 24
    for claim in library.annotations:
        assert claim["claim_type"] in {"FACT", "ASSESSMENT", "FORECAST", "SCENARIO"}
        assert claim["human_verified"] is False
        doc = next(d for d in library.documents if d["document_id"] == claim["document_id"])
        assert " ".join(claim["text"].split()) in " ".join(doc["pages"][claim["page"] - 1]["text"].split())
        for raw in [claim["time_scope"]["raw"], claim["modality_raw"], *claim["conditions"]]:
            if raw:
                assert raw in claim["text"]
    assert not library.search_claims("ксилофоноградквазитест")
    assert library.search_claims("корпоративный переменный купон ключевая ставка")


@pytest.mark.parametrize("role", ["cfo", "treasurer"])
@pytest.mark.parametrize("variant", ["floating", "fixed", "unknown"])
@pytest.mark.parametrize("event_index", [0, 1, 2, 3])
def test_new_primary_events_across_companies_and_roles_use_contract_and_bank(
    service, monkeypatch, role, variant, event_index
):
    event = json.loads((ROOT / "data/evaluation/funding_events.json").read_text())[event_index]
    event = {
        **event,
        "news_id": event["source_id"],
        "publisher": "Банк России",
        "read_status": "article_read",
        "excerpt": event["text"][:1000],
    }
    cid = service.workspace.create_company(
        {
            "floating": "Синтетический завод",
            "fixed": "Синтетический ритейлер",
            "unknown": "Синтетическая логистика",
        }[variant]
    )["company_id"]
    service.context_dialogue.save_fields(
        cid,
        ContextConfirm(
            expected_revision=0,
            fields=[
                {
                    "key": "debt_contract",
                    "category": "finance",
                    "label": "Договор",
                    "value": terms_json(terms(variant)),
                }
            ],
        ),
    )
    service.research.bank = BankLibrary(ROOT)
    pool(service, cid, [event])
    monkeypatch.setattr("alfa_agent.research.utcnow", lambda: datetime.fromisoformat(ASOF))
    request = ResearchRequest(
        company_id=cid,
        role=role,
        question="Как новость влияет на условия финансирования и процентные расходы?",
        news_text=NEWS,
        news_item_id=event["news_id"],
        public_search=False,
        planning_days=90,
    )
    job = service.research.create(request, "p1-matrix")
    asyncio.run(ResearchEngine(service.research, ResearchGateway()).once())
    result = service.research.get(job["run_id"])
    assert result["status"] in {"PARTIAL", "SUCCEEDED"}, (result["error_code"], result["trace"][-3:])
    r = result["result"]
    assert r["narrative"]["generation_mode"] == "corporate_funding_v1"
    assert r["funding_analysis"]["rate_type"] == variant and r["funding_analysis"]["bank_basis"]
    assert r["funding_analysis"]["source_anchor_ids"]
    assert (
        r["funding_analysis"]["news_lens"]["category"]
        == ["policy_outlook", "deposit_monitoring", "rate_cut", "rate_pause"][event_index]
    )
    if variant == "fixed":
        assert all(s["delta_contract_rate_pp"] == "0" for s in r["funding_analysis"]["scenarios"])
    assert r["synthetic_context"]

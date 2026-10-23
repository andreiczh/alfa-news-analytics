from __future__ import annotations

import asyncio
import csv
import json
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import zipfile

from fastapi.testclient import TestClient
from openpyxl import Workbook
from pypdf import PdfWriter
import pytest

from alfa_agent.api import create_app
from alfa_agent.case_data import CaseData
from alfa_agent.document_parser import ParseError, parse_document
from alfa_agent.engine import Engine
from alfa_agent.ingestion import DocumentProcessor, ExtractedFact, validate_fact
from alfa_agent.service import CreateRun, Service
from alfa_agent.settings import Settings
from alfa_agent.storage import Conflict
from alfa_agent.workspace import UploadError
from conftest import ScenarioGateway


@pytest.fixture
def case_service(tmp_path, monkeypatch):
    from alfa_agent.case_data import LABELS, FLAGS

    headers = ["SYNTHETIC_U", *LABELS, *FLAGS, "REVENUE_COMPANY"]
    data = tmp_path / "case"
    data.mkdir()
    blank = {key: ("0.0" if key in FLAGS else "Тестовое значение") for key in headers}
    base = {
        **blank,
        "SYNTHETIC_U": "U_TEST_A",
        "CL_TYPE": "CLIENT",
        "MAINOKVED_CODE": "10.1",
        "MAINOKVED_NAME": "Производство продуктов",
        "CL_PRODUCT_LOA_MARK": "1.0",
        "CL_PRODUCT_DEP_MARK": "0.0",
        "REVENUE_COMPANY": "1000.0",
        "FTS_FLAG": "1.0",
        "FTS_COUNTRY_NAME": "Китай",
        "NEGATIVEMARK": "1.0",
    }
    companies = [
        base,
        {
            **base,
            "SYNTHETIC_U": "U_TEST_B",
            "CL_PRODUCT_LOA_MARK": "0.0",
            "CL_PRODUCT_DEP_MARK": "1.0",
            "REVENUE_COMPANY": "2000000.0",
        },
        {**base, "SYNTHETIC_U": "U_COLLISION", "MAINOKVED_NAME": "Деятельность А"},
        {**base, "SYNTHETIC_U": "U_COLLISION", "MAINOKVED_NAME": "Деятельность Б"},
    ]
    with (data / "df_Uid_data_hack.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter=";")
        w.writeheader()
        w.writerows(companies)
    payments = [
        ["U_TEST_B", "U_TEST_A", "100.00", "10"],
        ["U_TEST_B", "U_TEST_A", "100.00", "10"],
        ["U_UNKNOWN", "U_TEST_A", "50.00", "1"],
        ["U_TEST_A", "U_TEST_B", "75.00", "3"],
        ["U_TEST_A", "U_TEST_A", "1000.00", "5"],
    ]
    with (data / "df_Uid_payment_hack.csv").open("w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["BENEFICIARY", "PAYER", "PAY_AMT", "PAY_CNT"])
        w.writerows(payments)
    with (data / "df_Fid_Uid_link_hack.csv").open("w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U"])
        w.writerows(
            [
                ["F_TEST", "Город обслуживания", "Регион обслуживания", "U_TEST_A"],
                ["F_TEST", "Город обслуживания", "Регион обслуживания", "U_TEST_B"],
            ]
        )
    monkeypatch.setenv("ALFA_CASE_DATA_DIR", str(data))
    (tmp_path / "data").mkdir()
    shutil.copy2(Path(__file__).parents[1] / "data/demo_bundle.json", tmp_path / "data/demo_bundle.json")
    service = Service(Settings(root=tmp_path))
    yield service
    service.corpus.close()


def by_name(profile, name):
    return next(f for f in profile["features"] if f["name"] == name)


def test_prebuilt_case_index_is_read_only_and_source_bound(case_service):
    original = case_service.workspace.case
    target = case_service.settings.root / "deploy/case-index/case.sqlite3"
    target.parent.mkdir(parents=True)
    with original.connect() as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    packaged = CaseData(case_service.settings)
    assert packaged.prebuilt
    assert packaged.summary()["row_counts"] == original.summary()["row_counts"]
    assert packaged.build("U_TEST_A")["profile"] == original.build("U_TEST_A")["profile"]
    with packaged.connect() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("CREATE TABLE should_not_exist(value TEXT)")
    with (original.folder / "df_Uid_data_hack.csv").open("a") as file:
        file.write("\n")
    with pytest.raises(ValueError, match="packaged_case_index_source_mismatch"):
        CaseData(case_service.settings)


def test_import_preserves_ids_flags_missingness_and_source_rows(case_service):
    data = case_service.workspace.case
    assert data.summary()["company_count"] == 3
    assert data.search("производство")["total"] == 2
    built = data.build("U_TEST_A")
    p = built["profile"]
    assert by_name(p, "has_loan")["boolean_value"] is True
    assert by_name(p, "owner_information_flag")["boolean_value"] is True
    assert by_name(p, "rate_type")["status"] == "missing"
    assert by_name(p, "floating_debt_rub")["numeric_value"] is None
    assert by_name(p, "revenue_source_units")["status"] == "missing"
    assert built["evidence"][0]["locator"].startswith("Строка 2;")
    assert len(built["evidence"]) > 20
    assert not any(f["name"] == "fx_exposure" for f in p["features"])


def test_conflicting_uid_never_selects_arbitrary_industry(case_service):
    built = case_service.workspace.case.build("U_COLLISION")
    assert built["profile"]["unresolved_identity"] is True
    industry = by_name(built["profile"], "industry")
    assert industry["status"] == "conflicting" and industry["text_value"] is None
    assert len(industry["source_refs"]) == 2


def test_payment_aggregation_keeps_repeats_and_unknown_profiles(case_service):
    links = case_service.workspace.case.connections("U_TEST_A")
    assert Decimal(links["outgoing"]["amount"]) == 250
    assert links["outgoing"]["payment_count"] == 21
    assert Decimal(links["incoming"]["amount"]) == 75
    assert Decimal(links["self_transfers"]["amount"]) == 1000
    assert links["outgoing"]["top"][0]["source_rows"] == 2
    assert links["outgoing"]["top"][0]["share_percent"] == "80.00"
    assert links["outgoing"]["top"][1]["profile_available"] is False
    assert links["period"] is None
    assert len(links["people_links"]) == 1


def test_case_loan_is_not_floating_debt_and_deposit_changes_rule(case_service):
    a = case_service.create(CreateRun(company_id="U_TEST_A"), "case-a")
    gateway = ScenarioGateway()
    asyncio.run(
        Engine(case_service.settings, case_service.store, case_service.corpus, gateway).once(a["run_id"])
    )
    result = case_service.get(a["run_id"])
    assert result["status"] == "PARTIAL"
    assert not result["result"]["calculations"]
    assert result["question"] is None
    assert result["result"]["company_context"]["source"] == "case_csv"
    assert "синтетическ" not in " ".join(result["result"]["core"]["limitations"]).lower()
    b = case_service.create(CreateRun(company_id="U_TEST_B"), "case-b")
    asyncio.run(
        Engine(case_service.settings, case_service.store, case_service.corpus, ScenarioGateway()).once(
            b["run_id"]
        )
    )
    state = case_service.store.get(b["run_id"])["state"]
    assert state["analysis"]["roles"][0]["mechanism"] == "deposit_budget"


class DocumentGateway:
    mode = "test_double"

    def __init__(self, amount="800000000"):
        self.amount = amount
        self.calls = []

    async def complete(self, job_id, stage, payload, schema, repair=None, **kwargs):
        self.calls.append(stage)
        assert stage == "profile_extractor"
        chunk = payload["chunks"][0]
        raw = [
            {"name": "rate_type", "value": "floating", "quote": "Тип ставки: плавающая."},
            {
                "name": "floating_debt_rub",
                "value": self.amount,
                "raw_value": self.amount,
                "quote": f"Остаток плавающего долга: {self.amount} руб.",
            },
        ]
        return json.dumps(
            {"facts": [{**f, "chunk_id": chunk["id"]} for f in raw], "issues": []}, ensure_ascii=False
        )


def upload_and_process(service, cid, amount="800000000", name="report.txt"):
    body = f"Тип ставки: плавающая.\nОстаток плавающего долга: {amount} руб."
    info, _ = service.workspace.add_file(cid, name, body.encode())
    gateway = DocumentGateway(amount)
    asyncio.run(DocumentProcessor(service.workspace, gateway).once())
    return service.workspace.file_info(cid, info["document_id"]), gateway


def test_document_pipeline_builds_profile_with_verbatim_evidence(service):
    cid = service.workspace.create_company("Документная компания — тест")["company_id"]
    info, gateway = upload_and_process(service, cid)
    assert info["status"] == "READY"
    assert info["feature_count"] == 2
    assert gateway.calls == ["profile_extractor"]
    snap = service.workspace.snapshot(cid, service.corpus)
    assert snap["profile"]["version"] == 2
    debt = by_name(snap["profile"], "floating_debt_rub")
    assert Decimal(debt["numeric_value"]["value"]) == 800000000
    assert debt["status"] == "self_reported"
    ev = next(e for e in snap["evidence"] if e["evidence_id"] == debt["source_refs"][0]["ref_id"])
    assert ev["quote"] == "Остаток плавающего долга: 800000000 руб."
    assert all(d["tenant_company_id"] == cid for d in snap["documents"])


def test_new_document_is_not_silently_applied_to_an_old_run(service):
    cid = service.workspace.create_company("Версии документов — тест")["company_id"]
    old = service.create(CreateRun(company_id=cid), "before-upload")
    info, _ = upload_and_process(service, cid)
    assert info["status"] == "READY"
    asyncio.run(
        Engine(service.settings, service.store, service.corpus, ScenarioGateway()).once(old["run_id"])
    )
    previous = service.get(old["run_id"])
    assert previous["result"]["profile_version"] == 1
    assert not previous["result"]["calculations"]
    new = service.create(CreateRun(company_id=cid), "after-upload")
    asyncio.run(
        Engine(service.settings, service.store, service.corpus, ScenarioGateway()).once(new["run_id"])
    )
    current = service.get(new["run_id"])
    assert current["status"] == "SUCCEEDED"
    assert current["result"]["profile_version"] == 2
    assert Decimal(current["result"]["role_views"][0]["scenario"]["amount_rub"]) == Decimal("-1972602.74")
    assert service.get(old["run_id"])["result"]["profile_version"] == 1


def test_conflicting_document_amounts_are_not_averaged(service):
    cid = service.workspace.create_company("Противоречия — тест")["company_id"]
    upload_and_process(service, cid, "800000000", "first.txt")
    upload_and_process(service, cid, "900000000", "second.txt")
    snap = service.workspace.snapshot(cid, service.corpus)
    debt = by_name(snap["profile"], "floating_debt_rub")
    assert debt["status"] == "conflicting" and debt["numeric_value"] is None
    assert len(debt["source_refs"]) == 2


def test_upload_deduplicates_same_company_but_never_cross_company(service):
    cid = service.workspace.create_company("Компания 1")["company_id"]
    other = service.workspace.create_company("Компания 2")["company_id"]
    first, created = service.workspace.add_file(cid, "../report.txt", b"Hello source")
    again, created_again = service.workspace.add_file(cid, "renamed.txt", b"Hello source")
    second, created_second = service.workspace.add_file(other, "report.txt", b"Hello source")
    assert created and not created_again and created_second
    assert first["document_id"] == again["document_id"] != second["document_id"]
    assert first["name"] == "report.txt"
    with pytest.raises(Exception, match="document_not_found"):
        service.workspace.file_row(other, first["document_id"])
    with pytest.raises(UploadError):
        service.workspace.add_file(cid, "payload.html", b"<script>alert(1)</script>")


def test_pending_document_blocks_new_analysis_but_not_idempotent_replay(service):
    old = service.create(CreateRun(), "repeat-with-upload")
    service.workspace.add_file("demo_floating", "new.txt", b"Unread document")
    assert service.create(CreateRun(), "repeat-with-upload")["run_id"] == old["run_id"]
    with pytest.raises(Conflict, match="company_documents_processing"):
        service.create(CreateRun(), "new-with-upload")


def test_agent_and_document_workers_share_a_single_execution_slot(service):
    service.workspace.add_file("demo_floating", "new.txt", b"Source")
    job = service.workspace.claim_document()
    assert job is not None
    # Direct storage call creates queued analysis independently of Service's pending-doc guard.
    from alfa_agent.engine import initial_state
    from alfa_agent.storage import new_id

    run_id = new_id("run")
    state = initial_state(run_id)
    service.store.create(
        "demo", "demo_floating", "slot", {"company_id": "demo_floating"}, state.model_dump(mode="json")
    )
    assert service.store.claim() is None
    service.workspace.update_job(job["id"], "complete", {}, status="READY")
    assert service.store.claim() is not None
    assert service.workspace.claim_document() is None


def fact(**kw):
    return ExtractedFact(
        name="floating_debt_rub",
        value="1200000000",
        raw_value="1,2",
        chunk_id="chunk_1",
        quote="Остаток плавающего долга 1,2 млрд руб.",
        **kw,
    )


def test_money_normalization_is_checked_not_trusted():
    candidate = fact()
    chunk = {"text": candidate.quote, "locator": "Страница 1"}
    f, _ = validate_fact(candidate, chunk, "upload_test", "company_test", 1)
    assert f.numeric_value.value == 1200000000
    with pytest.raises(ValueError, match="scale_mismatch"):
        validate_fact(
            candidate.model_copy(update={"value": "1200000"}), chunk, "upload_test", "company_test", 1
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"quote": "Придуманная цитата"}, "quote_not_verbatim"),
        ({"value": "0", "raw_value": "0"}, "numeric_raw_value_not_in_quote"),
        ({"unit_context": "В долларах"}, "unit_context_not_verbatim"),
        ({"period_raw": "2027"}, "period_not_in_source"),
    ],
)
def test_unsupported_extraction_candidates_blocked(change, code):
    candidate = fact()
    chunk = {"text": candidate.quote, "locator": "Страница 1"}
    with pytest.raises(ValueError, match=code):
        validate_fact(candidate.model_copy(update=change), chunk, "upload_test", "company_test", 1)


def test_unknown_currency_and_ranges_do_not_become_rubles():
    for quote in ["Остаток долга: 1,2 млрд USD.", "Остаток долга: от 1,2 млрд руб."]:
        candidate = fact().model_copy(update={"quote": quote})
        with pytest.raises(ValueError):
            validate_fact(candidate, {"text": quote, "locator": "Источник"}, "upload_test", "company_test", 1)


def test_read_text_office_tables_and_pdf_limits(tmp_path):
    textfile = tmp_path / "profile.txt"
    textfile.write_text("Компании: русский текст")
    assert "русский" in parse_document(textfile)["chunks"][0]["text"]
    office = tmp_path / "profile.docx"
    with zipfile.ZipFile(office, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Остаток долга: 1 млрд руб.</w:t></w:r></w:p></w:body></w:document>',
        )
    assert "1 млрд" in parse_document(office)["chunks"][0]["text"]
    slides = tmp_path / "profile.pptx"
    with zipfile.ZipFile(slides, "w") as z:
        z.writestr(
            "ppt/slides/slide1.xml",
            '<a:p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:r><a:t>Финансирование компании</a:t></a:r></a:p>',
        )
    assert parse_document(slides)["chunks"][0]["page"] == 1
    book = Workbook()
    sheet = book.active
    sheet.append(["Показатель", "RUB"])
    sheet.append(["Долг", 800000000])
    xlsx = tmp_path / "profile.xlsx"
    book.save(xlsx)
    assert "800000000" in parse_document(xlsx)["chunks"][0]["text"]
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    pdf = tmp_path / "scan.pdf"
    writer.write(pdf)
    assert parse_document(pdf)["ocr_pages"] == [1]
    writer.encrypt("secret")
    locked = tmp_path / "locked.pdf"
    writer.write(locked)
    with pytest.raises(ParseError, match="encrypted_document"):
        parse_document(locked)


def test_xml_entities_and_fake_text_file_rejected(tmp_path):
    p = tmp_path / "bad.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", '<!DOCTYPE a [<!ENTITY x "boom">]><a>&x;</a>')
    with pytest.raises(ParseError, match="xml_entities_not_allowed"):
        parse_document(p)
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\0\0\0")
    with pytest.raises(ParseError, match="binary_file_as_text"):
        parse_document(binary)


def test_api_company_upload_binding_download_and_favorites(service):
    app = create_app(service.settings)
    with TestClient(app) as client:
        h = {"X-App-Token": app.state.token}
        one = client.post("/api/companies", headers=h, json={"name": "Компания тест 1"}).json()["company_id"]
        two = client.post("/api/companies", headers=h, json={"name": "Компания тест 2"}).json()["company_id"]
        route = f"/api/companies/{one}/documents"
        bad = client.post(
            route,
            headers=h,
            data={"bound_company_id": two},
            files={"file": ("a.txt", b"Source", "text/plain")},
        )
        assert bad.status_code == 422
        good = client.post(
            route,
            headers=h,
            data={"bound_company_id": one},
            files={"file": ("a.txt", b"Source", "text/plain")},
        )
        assert good.status_code == 202
        did = good.json()["document"]["document_id"]
        assert client.get(f"{route}/{did}/download", headers=h).content == b"Source"
        assert client.get(f"/api/companies/{two}/documents/{did}/download", headers=h).status_code == 404
        assert client.get(f"/api/companies/{one}/profile", headers=h).json()["profile"]["company_id"] == one
        fav = f"/api/companies/{one}/favorites/rate_decision_20260911"
        assert client.put(fav, headers=h, json={}).status_code == 200
        assert len(client.get(f"/api/companies/{one}/favorites", headers=h).json()) == 1
        assert client.delete(fav, headers=h).status_code == 200
        assert not client.get(f"/api/companies/{one}/favorites", headers=h).json()
        assert client.get("/api/workspace", headers=h).status_code == 200
        assert len(client.get("/api/library", headers=h).json()["claims"]) == 3


def test_excluding_a_conflicting_document_restores_source_scoped_profile(service):
    cid = service.workspace.create_company("Согласование источников — тест")["company_id"]
    one, _ = upload_and_process(service, cid, "800000000", "one.txt")
    two, _ = upload_and_process(service, cid, "900000000", "two.txt")
    before = service.workspace.snapshot(cid, service.corpus, persist=True)
    service.corpus.register_snapshot(before)
    assert by_name(before["profile"], "floating_debt_rub")["status"] == "conflicting"
    service.workspace.set_file_active(cid, two["document_id"], False)
    after = service.workspace.snapshot(cid, service.corpus, persist=True)
    service.corpus.register_snapshot(after)
    debt = by_name(after["profile"], "floating_debt_rub")
    assert debt["numeric_value"]["value"] == "800000000"
    assert two["document_id"] not in service.corpus.documents
    assert two["document_id"] not in {
        d.document_id
        for d in service.corpus.search(
            "плавающий долг", "document", cid, service.corpus.as_of(after["profile"]["as_of"])
        )
    }
    assert service.workspace.file_info(cid, two["document_id"])["status"] == "EXCLUDED"
    assert service.workspace.file_row(cid, two["document_id"])["stored_name"]
    old = service.workspace.load_snapshot(before["snapshot_id"], cid)
    assert by_name(old["profile"], "floating_debt_rub")["status"] == "conflicting"
    service.corpus.register_snapshot(old)
    assert two["document_id"] in service.corpus.documents
    service.workspace.set_file_active(cid, two["document_id"], True)
    restored = service.workspace.snapshot(cid, service.corpus)
    assert by_name(restored["profile"], "floating_debt_rub")["status"] == "conflicting"


def test_numeric_fact_requires_raw_even_for_day_and_fraction_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="raw_value is required"):
        ExtractedFact(name="inventory_days", value="45", chunk_id="chunk_1", quote="Запасы: 45 дней.")
    with pytest.raises(ValidationError, match="raw_value is required"):
        ExtractedFact(
            name="credit_sales_share", value="0.6", chunk_id="chunk_1", quote="Продажи с отсрочкой: 60%."
        )


def test_reextracting_same_file_cannot_leak_new_evidence_into_old_snapshot(service):
    cid = service.workspace.create_company("Снимок одного файла — тест")["company_id"]
    info, _ = upload_and_process(service, cid)
    old = service.workspace.snapshot(cid, service.corpus)
    newer = json.loads(json.dumps(old))
    original = next(e for e in newer["evidence"] if e["document_id"] == info["document_id"])
    extra = {**original, "evidence_id": "newer_extraction_evidence"}
    newer["evidence"].append(extra)
    service.corpus.register_snapshot(newer)
    assert "newer_extraction_evidence" in service.corpus.evidence
    service.corpus.register_snapshot(old)
    assert "newer_extraction_evidence" not in service.corpus.evidence
    assert all(eid in service.corpus.evidence for eid in [e["evidence_id"] for e in old["evidence"]])

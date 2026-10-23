"""Durable document jobs. Model proposes facts, server binds quotes/units/ownership."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .document_parser import MAX_TEXT, packetize, render_page
from .domain import Citation, DocumentRef, Evidence, FeatureCandidate, NumericValue, TimeScope
from .gateway import GatewayError
from .settings import utcnow

FIELDS = Literal[
    "company_name",
    "industry",
    "description",
    "rate_type",
    "floating_debt_rub",
    "total_debt_rub",
    "capex_planned_rub",
    "inventory_days",
    "receivables_days",
    "credit_sales_share",
    "annual_revenue_rub",
    "ebitda_rub",
    "export_share",
    "import_share",
    "key_products",
    "markets",
    "employees",
    "business_model",
    "international_activity",
    "competitors",
    "strategy",
    "tactics",
    "funding_structure",
    "repricing_gap",
    "deposit_beta",
    "liquidity_buffer",
    "net_interest_income",
    "capital_adequacy",
]
MONEY = {"floating_debt_rub", "total_debt_rub", "capex_planned_rub", "annual_revenue_rub", "ebitda_rub"}
RATIOS = {"credit_sales_share", "export_share", "import_share"}
QUANTITIES = {"inventory_days", "receivables_days", "employees"}


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: FIELDS
    value: str = Field(min_length=1, max_length=1000)
    chunk_id: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=700)
    raw_value: str | None = Field(default=None, max_length=80)
    unit_context: str | None = Field(default=None, max_length=300)
    period_raw: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def numeric_raw_is_required(self):
        if self.name in MONEY | RATIOS | QUANTITIES and not self.raw_value:
            raise ValueError(
                "raw_value is required for every numeric fact, including days, people and shares; copy the numeric token verbatim from quote"
            )
        return self


class ExtractedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ExtractedFact] = Field(max_length=24)
    issues: list[str] = Field(default_factory=list, max_length=10)


class OCRPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=20000)
    unreadable: bool


def normalized_number(raw):
    return Decimal(
        raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".").replace("−", "-")
    )


def validate_fact(fact, chunk, document_id, company_id, index):
    if fact.quote not in chunk["text"]:
        raise ValueError("quote_not_verbatim")
    if fact.unit_context and fact.unit_context not in chunk["text"]:
        raise ValueError("unit_context_not_verbatim")
    if fact.period_raw and fact.period_raw not in chunk["text"]:
        raise ValueError("period_not_in_source")
    source = fact.quote + " " + (fact.unit_context or "")
    kw = {}
    if fact.name in MONEY | RATIOS | QUANTITIES:
        if not fact.raw_value or not re.search(
            r"(?<![\d.,])" + re.escape(fact.raw_value) + r"(?![\d.,])", fact.quote
        ):
            raise ValueError("numeric_raw_value_not_in_quote")
        # A range or approximation must not be turned into an exact point.
        if re.search(
            r"(?:от\s+\d|до\s+\d|около\s+\d|более\s+\d|менее\s+\d|\d\s*[–—]\s*\d|\d\s*-\s*\d)",
            fact.quote,
            re.I,
        ):
            raise ValueError("numeric_range_or_approximation")
        try:
            number = normalized_number(fact.value)
            raw = normalized_number(fact.raw_value)
        except InvalidOperation:
            raise ValueError("numeric_format_invalid") from None
        if not number.is_finite() or not raw.is_finite():
            raise ValueError("numeric_not_finite")
        if fact.name in MONEY:
            subjects = {
                "floating_debt_rub": r"плавающ|переменн|floating",
                "total_debt_rub": r"долг|кредит|за[её]м|обязательств|debt|loan",
                "capex_planned_rub": r"инвестиц|капитал|CAPEX",
                "annual_revenue_rub": r"выручк|revenue",
                "ebitda_rub": r"EBITDA",
            }
            if not re.search(subjects[fact.name], source, re.I):
                raise ValueError("financial_subject_not_explicit")
            if fact.name == "capex_planned_rub" and not re.search(
                r"план|бюджет|намеч|planned|forecast", source, re.I
            ):
                raise ValueError("investment_plan_not_explicit")
            if fact.name != "capex_planned_rub" and re.search(
                r"ожида|прогноз|планиру|будет|projected|forecast", source, re.I
            ):
                raise ValueError("forecast_is_not_observed_value")
            if not re.search(r"руб|₽|\bRUB\b", source, re.I) or re.search(
                r"доллар|USD|EUR|евро|\$", source, re.I
            ):
                raise ValueError("currency_not_explicit_rub")
            adjacent = re.search(
                re.escape(fact.raw_value) + r"\s*(млрд|млн|тыс|billion|million|thousand)?\s*(?:руб|₽|RUB)",
                fact.quote,
                re.I,
            )
            scale_text = adjacent.group(1) or "" if adjacent else fact.unit_context or source
            scale_matches = {
                Decimal(10) ** power
                for pattern, power in [(r"млрд|billion", 9), (r"млн|million", 6), (r"тыс|thousand", 3)]
                if re.search(pattern, scale_text, re.I)
            }
            if len(scale_matches) > 1:
                raise ValueError("numeric_scale_ambiguous")
            scales = scale_matches or {Decimal(1)}
            if (
                number not in {raw * s for s in scales}
                or abs(number) > Decimal("1e15")
                or (number < 0 and fact.name != "ebitda_rub")
            ):
                raise ValueError("numeric_scale_mismatch")
            unit = "RUB"
        elif fact.name in RATIOS:
            expected = raw / 100 if re.search(r"%|процент", source, re.I) else raw
            if number != expected or not 0 <= number <= 1:
                raise ValueError("ratio_scale_mismatch")
            unit = "fraction"
        else:
            if number != raw or not 0 <= number <= 1_000_000:
                raise ValueError("quantity_mismatch")
            if fact.name in {"inventory_days", "receivables_days"} and not re.search(
                r"дн[еёяи]|дней|сут[ок]|days?", source, re.I
            ):
                raise ValueError("day_unit_not_explicit")
            if fact.name == "employees" and not re.search(
                r"сотрудник|работник|численност|employees|people|чел", source, re.I
            ):
                raise ValueError("headcount_not_explicit")
            unit = "people" if fact.name == "employees" else "days"
        kw["numeric_value"] = NumericValue(
            value=number, unit=unit, raw=fact.raw_value + " " + (fact.unit_context or "")
        )
    elif fact.name == "rate_type":
        allowed = {
            "floating": r"плавающ|переменн|floating",
            "fixed": r"фиксирован|fixed",
            "mixed": r"смешанн|фиксирован.+плавающ|плавающ.+фиксирован",
            "none": r"нет.+(?:долг|кредит)|(?:долг|кредит).+отсутств",
        }
        if fact.value not in allowed or not re.search(allowed[fact.value], fact.quote, re.I | re.S):
            raise ValueError("rate_type_not_explicit")
        kw["text_value"] = fact.value
    else:
        if fact.value.casefold() not in fact.quote.casefold():
            raise ValueError("text_value_not_verbatim")
        kw["text_value"] = fact.value
    eid = f"{document_id}_ev_{index}"
    evidence = Evidence(
        evidence_id=eid,
        document_id=document_id,
        page=chunk.get("page"),
        object_kind="text",
        locator=chunk["locator"] + ("; единица: " + fact.unit_context if fact.unit_context else ""),
        quote=fact.quote,
        extraction_method="vision" if chunk.get("method") == "vision" else "text",
    )
    feature = FeatureCandidate(
        feature_id=f"{document_id}_feature_{index}",
        name=fact.name,
        status="self_reported",
        period=TimeScope(raw=fact.period_raw),
        source_refs=[Citation(kind="evidence", ref_id=eid)],
        note="Из документа, загруженного для этой компании. Извлечение ИИ; требует проверки по оригиналу.",
        **kw,
    )
    return feature, evidence


def parse_in_process(path):
    try:
        outcome = subprocess.run(
            [sys.executable, "-m", "alfa_agent.document_parser", str(path)],
            capture_output=True,
            timeout=40,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("file_parse_timeout") from None
    if len(outcome.stdout) > 1_000_000:
        raise ValueError("document_text_limit")
    try:
        body = json.loads(outcome.stdout)
    except ValueError:
        raise ValueError("file_parse_failed") from None
    if outcome.returncode or body.get("error"):
        raise ValueError(body.get("error", "file_parse_failed"))
    return body


class DocumentProcessor:
    def __init__(self, workspace, gateway):
        self.workspace, self.gateway = workspace, gateway
        self.store = workspace.store

    async def once(self):
        job = self.workspace.claim_document()
        if not job:
            return None
        await self.execute(job)
        return job["id"]

    async def execute(self, job):
        progress = json.loads(job["progress_json"])
        try:
            row = self.workspace.file_row(job["company_id"], job["document_id"])
            path = self.workspace.upload_dir / row["stored_name"]
            if "parsed" not in progress:
                progress["parsed"] = await asyncio.to_thread(parse_in_process, path)
                progress["ocr_done"] = []
                progress["facts"] = []
                progress["processed_packets"] = 0
                self.workspace.update_job(job["id"], "extract", progress)
            parsed = progress["parsed"]
            for page in parsed["ocr_pages"]:
                if page in progress["ocr_done"]:
                    continue
                self.workspace.update_job(job["id"], "ocr", progress)
                image = await asyncio.to_thread(render_page, path, page)
                recognized = await self.invoke(
                    job,
                    "document_ocr",
                    {"document_id": job["document_id"], "page": page},
                    OCRPage,
                    images=[image],
                    model="qwen/qwen3-vl-30b-a3b-instruct",
                )
                if recognized.unreadable or not recognized.text.strip():
                    raise ValueError("document_page_unreadable")
                chunks = packetize(
                    [
                        {
                            "text": recognized.text,
                            "locator": f"Страница {page}, распознано ИИ",
                            "page": page,
                            "method": "vision",
                        }
                    ]
                )
                for chunk in chunks:
                    chunk["id"] = "ocr_" + str(page) + "_" + chunk["id"]
                parsed["chunks"].extend(chunks)
                if sum(len(c["text"]) for c in parsed["chunks"]) > MAX_TEXT:
                    raise ValueError("document_text_limit")
                progress["ocr_done"].append(page)
                self.workspace.update_job(job["id"], "extract", progress)
                self.consume(job["id"])
            chunks = parsed["chunks"]
            # Exact bounded packets, not silent truncation or top-k omission.
            packets = []
            for chunk in chunks:
                if packets and sum(len(c["text"]) for c in packets[-1]) + len(chunk["text"]) <= 12000:
                    packets[-1].append(chunk)
                else:
                    packets.append([chunk])
            progress["total_packets"] = len(packets)
            for index, packet in enumerate(packets):
                if index < progress["processed_packets"]:
                    continue
                self.workspace.update_job(job["id"], "extract", progress)
                facts = await self.invoke(
                    job,
                    "profile_extractor",
                    {
                        "company_id": job["company_id"],
                        "document_id": job["document_id"],
                        "filename": row["name"],
                        "chunks": packet,
                    },
                    ExtractedFacts,
                )
                progress["facts"].extend(f.model_dump(mode="json") for f in facts.facts)
                progress["processed_packets"] = index + 1
                self.workspace.update_job(job["id"], "validate", progress)
                self.consume(job["id"])
            features, evidence, issues = [], [], list(parsed["issues"])
            by_id = {c["id"]: c for c in chunks}
            seen = set()
            for index, raw in enumerate(progress["facts"], 1):
                fact = ExtractedFact.model_validate(raw)
                chunk = by_id.get(fact.chunk_id)
                if not chunk:
                    issues.append("Исключён кандидат: ссылка на неизвестный фрагмент.")
                    continue
                try:
                    feature, ev = validate_fact(fact, chunk, job["document_id"], job["company_id"], index)
                except ValueError as error:
                    issues.append("Исключён кандидат " + fact.name + ": " + str(error))
                    continue
                key = (fact.name, fact.value, fact.period_raw, fact.quote)
                if key in seen:
                    continue
                seen.add(key)
                features.append(feature)
                evidence.append(ev)
            if progress["ocr_done"]:
                issues.append(
                    "Есть страницы, распознанные разрешённой VL-моделью. Числа требуют проверки по изображению оригинала."
                )
            if not features:
                issues.append(
                    "Файл прочитан, но поддерживаемые показатели компании с проверяемыми цитатами не найдены."
                )
            # Readable source remains available to later tools even when no supported feature was found.
            for chunk in chunks[:12]:
                evidence.append(
                    Evidence(
                        evidence_id=job["document_id"] + "_excerpt_" + chunk["id"],
                        document_id=job["document_id"],
                        page=chunk.get("page"),
                        object_kind="text",
                        locator=chunk["locator"],
                        quote=chunk["text"][:1000],
                        extraction_method="vision" if chunk.get("method") == "vision" else "text",
                    )
                )
            document = DocumentRef(
                document_id=job["document_id"],
                source_kind="company_upload",
                title=row["name"],
                publisher="Документ, предоставленный пользователем компании",
                published_at=None,
                publication_date_raw=None,
                available_at=row["created_at"],
                sha256=row["sha256"],
                tenant_company_id=job["company_id"],
            )
            self.workspace.finish_document(job, document, chunks, features, evidence, issues)
            self.store.audit(
                job["id"],
                "PROFILE",
                "profile_updated",
                {
                    "company_id": job["company_id"],
                    "document_id": job["document_id"],
                    "features": len(features),
                    "issues": len(issues),
                },
            )
        except GatewayError as error:
            self.workspace.update_job(
                job["id"],
                "stopped",
                progress,
                "DEFERRED_BUDGET" if error.retry_at else "FAILED",
                error.code,
                error.retry_at,
            )
        except Exception as error:
            code = (
                str(error)
                if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]{3,80}", str(error))
                else "document_processing_failed"
            )
            self.workspace.update_job(job["id"], "stopped", progress, "FAILED", code)

    def consume(self, job_id):
        with self.store.connection() as db:
            db.execute(
                "UPDATE attempts SET status='consumed' WHERE run_id=? AND status='received'", (job_id,)
            )

    async def invoke(self, job, stage, payload, contract, **kwargs):
        repair = None
        for _ in range(2):
            if len(self.store.attempts(job["id"])) >= 24:
                raise GatewayError("document_call_limit")
            with self.store.connection() as db:
                db.execute(
                    "UPDATE document_jobs SET lease_until=? WHERE id=? AND status='RUNNING'",
                    (utcnow().timestamp() + self.workspace.settings.lease_seconds, job["id"]),
                )
            raw = await self.gateway.complete(
                job["id"], stage, payload, contract.model_json_schema(), repair=repair, **kwargs
            )
            try:
                return contract.model_validate_json(raw)
            except ValidationError as error:
                repair = {
                    "raw": raw,
                    "errors": error.errors(include_input=False, include_context=False, include_url=False),
                }
        raise GatewayError("document_contract_invalid")

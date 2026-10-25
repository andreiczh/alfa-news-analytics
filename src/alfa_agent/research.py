"""Own research controller: bounded planning, source tools, verification and replanning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .bank_library import BankLibrary, terms
from .gateway import GatewayError
from .public_sources import PublicSourceError, TOPICS, page_source
from .identity import role_for
from .news import news_source
from .news_sources import article_matches, article_publication
from .settings import utcnow
from .storage import Conflict, NotFound, new_id
from .analysis_policy import planning_policy, POLICY_VERSION
from .personalization import is_bank_company
from .research_rules import RULES, BANK_ROLES, COMPANY_ROLES
from .narrative import (
    NarrativeSelection,
    source_message_options,
    bind_source_messages,
    validate_narrative,
    bind_narrative,
    decision_schema,
    revision_details,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def fresh_news_intent(question):
    return bool(
        re.search(
            r"свеж|последн\w*\s+(?:новост|событ|изменени|публикац)|текущ\w*\s+(?:новост|ситуац)|что\s+происходит[^?]{0,100}сейчас|за\s+(?:(?:7|семь)\s+дн|неделю)",
            question,
            re.I,
        )
    )


class ResearchRequest(Strict):
    company_id: str = Field(max_length=100)
    question: str = Field(min_length=12, max_length=1500)
    job_title: str | None = Field(default=None, min_length=2, max_length=120)
    role: Literal["cfo", "treasurer", "ceo", "coo", "cco", "cmo", "other"] | None = None
    public_search: bool = True
    source_urls: list[str] = Field(default_factory=list, max_length=3)
    news_text: str = Field(default="", max_length=10000)
    parent_id: str | None = Field(default=None, max_length=100)
    bank_document_id: str | None = Field(default=None, max_length=100)
    news_item_id: str | None = Field(default=None, pattern=r"^news_[a-f0-9]{24}$")
    planning_days: Literal[7, 30, 90, 365] = 30
    allow_partial: bool = True


class ResearchAction(Strict):
    tool: Literal["company", "bank", "public", "read_source", "write", "ask_user"]
    reason: str = Field(min_length=5, max_length=350)
    query: str = Field(default="", max_length=350)
    topic: Literal[
        "company", "rates", "inflation", "demand", "labor", "commodities", "competitors", "regulation"
    ] = "company"
    source_id: str = Field(default="", max_length=100)
    questions: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("reason", mode="before")
    @classmethod
    def short_observation(cls, value):
        return value[:350] if isinstance(value, str) else value


class ResearchPlan(Strict):
    rationale: str = Field(max_length=600)
    actions: list[ResearchAction] = Field(min_length=1, max_length=5)

    @field_validator("rationale", mode="before")
    @classmethod
    def short_plan_note(cls, value):
        return value[:600] if isinstance(value, str) else value


class SourceQuote(Strict):
    anchor_id: str = Field(default="", max_length=20)
    source_id: str = Field(default="", max_length=100)
    quote: str = Field(default="", max_length=650)


class Finding(Strict):
    finding_id: str = Field(pattern=r"^f[1-9][0-9]?$", max_length=3)
    kind: Literal["source_fact", "bank_view", "analysis", "hypothesis"]
    statement_type: Literal["fact", "forecast", "assessment", "conditional"]
    text: str = Field(min_length=15, max_length=800)
    why_company: str = Field(min_length=10, max_length=500)
    consequence: str = Field(min_length=10, max_length=600)
    condition: str = Field(default="", max_length=400)
    priority: Literal["check_now", "monitor", "background"]
    citations: list[SourceQuote] = Field(min_length=1, max_length=3)


class ResearchRole(Strict):
    role: Literal["cfo", "treasurer", "ceo", "coo", "cco", "cmo"]
    focus: str = Field(min_length=5, max_length=150)
    interpretation: str = Field(min_length=10, max_length=600)
    actions: list[str] = Field(min_length=1, max_length=3)
    finding_ids: list[str] = Field(min_length=1, max_length=5)


class ResearchDraft(Strict):
    title: str = Field(min_length=10, max_length=160)
    summary: str = Field(min_length=20, max_length=850)
    findings: list[Finding] = Field(min_length=1, max_length=6)
    role_views: list[ResearchRole] = Field(min_length=1, max_length=6)
    narrative: dict | None = None
    missing_data: list[str] = Field(default_factory=list, max_length=7)
    limitations: list[str] = Field(default_factory=list, max_length=7)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=3)


class ResearchVerdict(Strict):
    approved: bool
    issues: list[str] = Field(default_factory=list, max_length=8)
    scope_note: str = Field(min_length=10, max_length=600)

    @field_validator("scope_note", mode="before")
    @classmethod
    def short_scope_note(cls, value):
        return value[:600] if isinstance(value, str) else value


class MechanismChoice(Strict):
    mechanism: Literal[
        "bank_alm",
        "bank_liquidity",
        "bank_credit_risk",
        "corporate_funding",
        "cash_cycle",
        "demand",
        "costs",
        "fx",
        "competition",
        "regulation",
    ]
    anchor_ids: list[str] = Field(min_length=1, max_length=2)
    priority: Literal["check_now", "monitor", "background"]


class ResearchSelection(Strict):
    findings: list[MechanismChoice] = Field(min_length=1, max_length=3)
    bank_anchor_id: str = ""


def selection_schema(ledger, bank_company):
    schema = ResearchSelection.model_json_schema()
    allowed = [
        k
        for k in RULES
        if (k not in {"corporate_funding", "cash_cycle"} if bank_company else not k.startswith("bank_"))
    ]
    props = schema["$defs"]["MechanismChoice"]["properties"]
    props["mechanism"]["enum"] = allowed
    props["anchor_ids"]["items"] = {"type": "string", "enum": [a["anchor_id"] for a in ledger]}
    schema["properties"]["bank_anchor_id"]["enum"] = [""] + [
        a["anchor_id"] for a in ledger if a["kind"] == "bank_claim"
    ]
    return schema


def topic_for_question(question):
    for topic, pattern in (
        ("rates", r"ставк|процент|марж|фондир|ликвид"),
        ("commodities", r"золот|нефт|металл|сталь|сырьев|сырьё"),
        ("competitors", r"конкурент|позиционир"),
        ("inflation", r"инфляц|затрат|тариф|себестоим"),
        ("demand", r"спрос|потребл|продаж|клиент|маркет|сегмент"),
        ("regulation", r"регулир|закон|санкц|норматив"),
        ("labor", r"сотрудник|персонал|зарплат|труд"),
    ):
        if re.search(pattern, question, re.I):
            return topic
    return "company"


def assemble_research(selection, ledger, request, snapshot):
    anchors = {a["anchor_id"]: a for a in ledger}
    context = next((a for a in ledger if a["kind"] == "company_evidence"), None)
    findings, needs, focuses, used = [], [], [], set()

    def citation(anchor):
        return SourceQuote(
            anchor_id=anchor["anchor_id"], source_id=anchor["source_id"], quote=anchor["quote"]
        )

    grouped = {}
    priority_order = ["check_now", "monitor", "background"]
    for choice in selection.findings:
        if not all(aid in anchors for aid in choice.anchor_ids):
            raise ValueError("unknown_citation_anchor")
        if choice.mechanism in grouped:
            previous = grouped[choice.mechanism]
            combined = list(dict.fromkeys(previous.anchor_ids + choice.anchor_ids))
            combined.sort(
                key=lambda aid: bool(
                    re.search(RULES[choice.mechanism]["keywords"], anchors[aid]["quote"], re.I)
                ),
                reverse=True,
            )
            previous.anchor_ids = combined[:2]
            previous.priority = min((previous.priority, choice.priority), key=priority_order.index)
        else:
            grouped[choice.mechanism] = choice.model_copy(deep=True)
    ordered_choices = sorted(grouped.values(), key=lambda choice: priority_order.index(choice.priority))
    for choice in ordered_choices:
        used.add(choice.mechanism)
        if request["bank_company"] != choice.mechanism.startswith("bank_") and choice.mechanism in {
            "bank_alm",
            "bank_liquidity",
            "bank_credit_risk",
            "corporate_funding",
            "cash_cycle",
        }:
            raise ValueError("mechanism_not_applicable_to_company_type")
        chosen = [anchors.get(x) for x in choice.anchor_ids]
        if not all(chosen):
            raise ValueError("unknown_citation_anchor")
        rule = RULES[choice.mechanism]
        if not re.search(rule["keywords"], " ".join(a["quote"] for a in chosen), re.I):
            raise ValueError("evidence_does_not_match_mechanism:" + choice.mechanism)
        if context and context not in chosen:
            chosen.append(context)
        findings.append(
            Finding(
                finding_id="f" + str(len(findings) + 1),
                kind="analysis",
                statement_type="conditional",
                text=rule["text"],
                why_company=rule["why"],
                consequence=rule["effect"],
                condition=rule["condition"],
                priority=choice.priority,
                citations=[citation(a) for a in chosen],
            )
        )
        needs.extend(rule["needs"])
        focuses.append(rule["focus"])
    selected_news = request.get("news_item_id")
    event_anchor = next(
        (
            a
            for a in ledger
            if a.get("news_item")
            and a["kind"] == "public_page"
            and (
                a["source_id"] == selected_news
                if selected_news
                else any(a["anchor_id"] in c.anchor_ids for c in ordered_choices)
            )
        ),
        None,
    )
    if selected_news:
        if not event_anchor:
            raise ValueError("selected_news_article_not_read")
        if not any(c.source_id == selected_news for f in findings for c in f.citations):
            raise ValueError("selected_news_must_ground_interpretation")
    if (
        request.get("fresh_news_requested")
        and not event_anchor
        and any(
            a.get("news_item") and a.get("provided_for_current_question") and a["kind"] == "public_page"
            for a in ledger
        )
    ):
        raise ValueError("fresh_news_must_ground_interpretation")
    short_quote = complete_news_excerpt(event_anchor) if event_anchor else None
    if short_quote:
        findings.append(
            Finding(
                finding_id="f" + str(len(findings) + 1),
                kind="source_fact",
                statement_type="assessment",
                text="В прочитанной публикации сообщается: «" + short_quote + "».",
                why_company="Это исходное событие для проверки выбранного канала влияния; наличие экспозиции компании отдельно не установлено.",
                consequence="Содержание публикации отделено от условной интерпретации и от датированной позиции банка.",
                priority="background",
                citations=[SourceQuote(source_id=event_anchor["source_id"], quote=short_quote)],
            )
        )
    limitations = [
        "Вывод построен как условный сценарий по проверяемым правилам; фактический знак и денежный эффект по компании не рассчитаны."
    ]
    selected_bank = selection.bank_anchor_id or next(
        (
            aid
            for c in selection.findings
            for aid in c.anchor_ids
            if anchors.get(aid, {}).get("kind") == "bank_claim"
        ),
        "",
    )
    if not selected_bank and used & {"bank_alm", "corporate_funding"}:
        selected_bank = next(
            (
                a["anchor_id"]
                for a in ledger
                if a["kind"] == "bank_claim" and a["source_id"] == "bank_assessment"
            ),
            "",
        )
    if selected_bank:
        anchor = anchors.get(selected_bank)
        if not anchor or anchor["kind"] != "bank_claim":
            raise ValueError("bank_anchor_not_attributed")
        ctype = "forecast" if anchor["claim_type"] == "FORECAST" else "assessment"
        stamp = (
            datetime.fromisoformat(anchor["published_at"]).strftime("%d.%m.%Y")
            if anchor.get("published_at")
            else "с неизвестной датой"
        )
        prefix = (
            "В обзоре от "
            + stamp
            + (" банк сформулировал прогноз" if ctype == "forecast" else " банк дал оценку")
        )
        findings.append(
            Finding(
                finding_id="f" + str(len(findings) + 1),
                kind="bank_view",
                statement_type=ctype,
                text=prefix + ": «" + anchor["quote"] + "». Это позиция на дату материала.",
                why_company="Датированный банковский взгляд задаёт основание для сценарной проверки; условия конкретной компании проверяются отдельно.",
                consequence="Если предпосылки обзора применимы к плановому периоду, их стоит сопоставить с внутренними сценариями; актуальность позиции не подтверждена.",
                condition="Сохранены исходный период и модальность; прогноз не является подтверждением свершившегося события.",
                priority="background",
                citations=[citation(anchor)],
            )
        )
    else:
        limitations.append(
            "Для выбранного вопроса не выбрана отдельная структурированная позиция банка; фрагменты OCR показаны как источники, их авторская атрибуция требует проверки."
        )
    known = {
        f["name"]
        for f in snapshot["profile"]["features"]
        if f["status"] not in {"missing", "conflicting"}
        and not re.search(r"не предостав|нет данн|неизвест|не указан", f.get("text_value") or "", re.I)
    }
    missing = list(dict.fromkeys(label for name, label in needs if name not in known))[:7]
    if snapshot.get("context", {}).get("synthetic"):
        limitations.append(
            "Профиль компании содержит явно тестовый контекст; он не является внутренними данными реального банка или клиента."
        )
    if not any(a["kind"] == "public_page" for a in ledger):
        limitations.append(
            "Свежие внешние страницы не прочитаны; исследование ограничено материалами кейса и компании."
        )
    if any(
        "синтетич" in a["quote"].casefold() for a in ledger if a["kind"] in {"company_evidence", "user_news"}
    ):
        limitations.append(
            "В контексте есть явно синтетический тестовый материал; он не подтверждает реальные финансовые показатели компании."
        )
    roles = []
    all_ids = [f.finding_id for f in findings if f.kind == "analysis"]
    bank_finance = request["bank_company"] and ordered_choices[0].mechanism.startswith("bank_")
    for role, (focus, first, second) in (BANK_ROLES if bank_finance else COMPANY_ROLES).items():
        roles.append(
            ResearchRole(
                role=role,
                focus=focus,
                interpretation="Применимость к задаче: "
                + "; ".join(focuses)
                + ". Решение зависит от подтверждённой экспозиции компании и условий выбранных источников.",
                actions=[first + ": " + focuses[0].lower(), second],
                finding_ids=all_ids,
            )
        )
    return ResearchDraft(
        title=focuses[0] + ": что проверить",
        summary="Приоритеты проверки: "
        + "; ".join(focuses)
        + ". Датированные рыночные материалы задают сценарии. Их применимость и размер эффекта для компании нужно сопоставить с внутренними данными.",
        findings=findings,
        role_views=roles,
        missing_data=missing,
        limitations=limitations,
        follow_up_questions=[
            "Какие внутренние данные помогут проверить этот сценарий?",
            "Какие предпосылки могут изменить вывод для нашей компании?",
        ],
    )


class ResearchAnswer(Strict):
    answer: str = Field(min_length=2, max_length=5000)
    expected_version: int = Field(ge=1)


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def numbers(value):
    date_tokens = []

    def calendar(match):
        date_tokens.extend(re.split(r"[./-]", match.group()))
        return " "

    without_dates = re.sub(r"\b(?:\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2})\b", calendar, value)
    tokens = re.findall(r"(?<![\w.,])[-+−]?\d+(?:[.,]\d+)?", without_dates) + date_tokens
    return {str(Decimal(x.replace(",", ".").replace("−", "-")).normalize()) for x in tokens}


def complete_news_excerpt(anchor):
    if anchor.get("headline_quote"):
        return anchor["headline_quote"]
    # Never cut a table cell, a unit or a condition at an arbitrary word count.
    for sentence in re.split(r"(?<=[.!?])\s+", anchor["quote"]):
        sentence = sentence.strip()
        if sentence and sentence[0].isupper() and sentence[-1] in ".!?" and len(sentence.split()) <= 24:
            return sentence
    return None


def news_headline_quote(source):
    title = re.sub(r"\s*\(\d{2}\.\d{2}\.\d{4}\)\s*$", "", source.get("title") or "").strip()
    if not title or len(title.split()) > 24:
        return None
    match = re.search(r"\s+".join(re.escape(word) for word in title.split()), source.get("text", ""), re.I)
    if match:
        return match.group(0)
    wanted = terms(title)
    sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", source.get("text", "")[:4000]):
        sentence = sentence.strip()
        if sentence and sentence[0].isupper() and sentence[-1] in ".!?" and len(sentence.split()) <= 24:
            score = len(terms(sentence) & wanted)
            if score:
                sentences.append((score, sentence))
    return max(sentences, key=lambda item: item[0])[1] if sentences else None


def evidence_passages(text, limit=580):
    """Keep sentence boundaries and exact source characters, rather than word windows.

    Oversized sentences are retained as retrieval fragments but excluded from a
    standalone source message. They can still ground a properly limited inference.
    """
    ends = [m.end() for m in re.finditer(r"(?<!\d)[.!?](?!\d)(?=\s|$)", text)]
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    start, group, group_start = 0, "", 0
    result = []
    for end in ends:
        sentence_start = start + len(text[start:end]) - len(text[start:end].lstrip())
        sentence = text[start:end].strip()
        start = end
        if not sentence:
            continue
        if len(sentence) > limit:
            if group:
                result.append((group, True))
                group = ""
            for offset in range(0, len(sentence), 480):
                begin = 0 if not offset else sentence.find(" ", offset)
                if begin < 0:
                    continue
                finish = min(begin + limit, len(sentence))
                if finish < len(sentence):
                    finish = max(begin, sentence.rfind(" ", begin, finish))
                result.append((sentence[begin:finish].strip(), False))
        elif group and len(text[group_start:end].strip()) > limit:
            result.append((group, True))
            group = sentence
            group_start = sentence_start
        else:
            # Preserve the source whitespace exactly when combining adjacent sentences.
            if not group:
                group_start = sentence_start
            group = text[group_start:end].strip()
    if group:
        result.append((group, True))
    return [(quote, complete) for quote, complete in result if len(quote) >= 30]


def evidence_ledger(sources, question):
    """The host owns quotations. The model selects small opaque anchors, not new quotes."""
    ledger, wanted = [], terms(question)
    ranked_sources = sorted(
        sources,
        key=lambda s: (
            bool(s.get("provided_for_current_question")),
            s["kind"] == "bank_claim",
            len(terms(s.get("text", "")) & wanted),
        ),
        reverse=True,
    )
    contexts = [s for s in sources if s["kind"] == "company_evidence" and s.get("context_key")]
    order = {"priorities": 0, "goals": 1, "risks": 2, "targets": 3, "commercial_priorities": 4}
    contexts.sort(key=lambda source: order.get(source.get("context_key"), 5))
    leading = contexts[:4] or [s for s in sources if s["kind"] == "company_evidence"][:1]
    ranked_sources = leading + [s for s in ranked_sources if s not in leading]
    for source in ranked_sources:
        if source["kind"] in {"url_to_read", "search_lead"}:
            continue
        text = source.get("text", "")
        candidates = []
        for quote, complete in evidence_passages(text):
            candidates.append((len(terms(quote) & wanted), quote, complete))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, quote, complete in candidates[:2]:
            ledger.append(
                {
                    "anchor_id": "a" + str(len(ledger) + 1),
                    "context_key": source.get("context_key"),
                    "source_id": source["source_id"],
                    "quote": quote,
                    "excerpt_complete": complete,
                    "kind": source["kind"],
                    "title": source.get("title"),
                    "published_at": source.get("published_at"),
                    "page": source.get("page"),
                    "claim_type": source.get("claim_type"),
                    "publisher": source.get("publisher"),
                    "news_item": source.get("news_item", False),
                    "headline_quote": news_headline_quote(source) if source.get("news_item") else None,
                    "provided_for_current_question": source.get("provided_for_current_question", False),
                    "bank_owner_explicit": source["kind"] == "bank_claim"
                    or (
                        source["kind"] == "bank_excerpt"
                        and bool(re.search(r"\b(мы|наш\w*|ожидаем|полагаем|считаем)\b", quote, re.I))
                    ),
                }
            )
        if len(ledger) >= 32:
            break
    return ledger[:32]


def bind_citations(draft, ledger):
    anchors = {a["anchor_id"]: a for a in ledger}
    for finding in draft.findings:
        for citation in finding.citations:
            if citation.anchor_id and citation.anchor_id in anchors:
                anchor = anchors[citation.anchor_id]
                citation.source_id, citation.quote = anchor["source_id"], anchor["quote"]


def validate_draft(draft, sources, *, bank_company=False):
    """Structural/evidence checks complement, and do not replace, semantic verification."""
    by_id = {s["source_id"]: s for s in sources}
    issues, found, quoted = [], {}, []
    for finding in draft.findings:
        if finding.finding_id in found:
            issues.append("duplicate_finding_id")
        found[finding.finding_id] = finding
        local = []
        explicit_bank_owner, bank_source_present = False, False
        for citation in finding.citations:
            source = by_id.get(citation.source_id)
            if not source or source["kind"] in {"search_lead", "url_to_read"}:
                issues.append("unread_or_unknown_source:" + citation.source_id)
                continue
            if normalized(citation.quote) not in normalized(source["text"]):
                issues.append("quote_not_in_source:" + citation.source_id)
                continue
            local.append(citation.quote)
            local.append(
                (source.get("published_at") or "") + " " + (source.get("publication_date_raw") or "")
            )
            quoted.append(citation.quote)
            quoted.append(local[-1])
            if finding.kind == "bank_view":
                if source["kind"] in {"bank_excerpt", "bank_claim"}:
                    bank_source_present = True
                    explicit_bank_owner |= source["kind"] == "bank_claim" or bool(
                        re.search(
                            r"\b(мы|наш\w*|ожидаем|полагаем|считаем|прогнозируем)\b", citation.quote, re.I
                        )
                    )
        if finding.kind == "bank_view" and not explicit_bank_owner:
            issues.append(
                "bank_owner_not_explicit_in_quote" if bank_source_present else "bank_owner_not_supported"
            )
        text = finding.text + " " + finding.why_company + " " + finding.consequence + " " + finding.condition
        if not numbers(text) <= numbers(" ".join(local)):
            issues.append("unsupported_number:" + finding.finding_id)
        if finding.kind in {"analysis", "hypothesis"} and finding.statement_type != "conditional":
            issues.append("interpretation_must_be_conditional:" + finding.finding_id)
        if finding.kind in {"analysis", "hypothesis"} and not finding.condition.strip():
            issues.append("interpretation_needs_condition:" + finding.finding_id)
        if finding.statement_type == "forecast" and finding.kind == "source_fact":
            issues.append("forecast_not_a_fact:" + finding.finding_id)
    if draft.narrative:
        for anchor in draft.narrative.get("evidence", []):
            source = by_id.get(anchor.get("source_id"))
            if not source or normalized(anchor.get("quote", "")) not in normalized(source.get("text", "")):
                issues.append("narrative_quote_not_in_source")
            quoted.append(anchor.get("quote", ""))
            quoted.append(anchor.get("published_at") or "")
    from .narrative import numeric_text

    if not numbers(numeric_text(draft.title + " " + draft.summary)) <= numbers(
        numeric_text(" ".join(quoted))
    ):
        issues.append("unsupported_summary_number")
    roles = set()
    for view in draft.role_views:
        if view.role in roles:
            issues.append("duplicate_role")
        roles.add(view.role)
        if any(fid not in found for fid in view.finding_ids):
            issues.append("unknown_role_finding")
            continue
        evidence = " ".join(
            c.quote + " " + (by_id.get(c.source_id, {}).get("published_at") or "")
            for fid in view.finding_ids
            for c in found[fid].citations
        )
        if not numbers(view.focus + " " + view.interpretation + " ".join(view.actions)) <= numbers(evidence):
            issues.append("unsupported_role_number:" + view.role)
    if bank_company and re.search(r"экономи\w* (?:на |по )?процент\w* расход\w*", draft.summary, re.I):
        issues.append("bank_alm_cannot_be_replaced_with_corporate_debt_formula")
    return list(dict.fromkeys(issues))


def semantic_verification_input(draft, sources):
    """Exact checked quotations once; source metadata without repeating full articles."""
    data = draft.model_dump(mode="json")
    used = {c.source_id for finding in draft.findings for c in finding.citations}
    if data.get("narrative"):
        evidence = data["narrative"].get("evidence", [])
        used.update(a["source_id"] for a in evidence)
        data["narrative"]["evidence"] = [
            {k: a[k] for k in ("anchor_id", "source_id", "quote", "page", "context_key") if k in a}
            for a in evidence
        ]
    metadata_keys = {
        "source_id",
        "kind",
        "title",
        "publisher",
        "published_at",
        "publication_date_raw",
        "url",
        "page",
        "review_status",
        "claim_type",
        "claim_owner",
        "forecast_horizon",
        "context_key",
        "limitations",
        "news_item",
        "provided_for_current_question",
        "bank_owner_explicit",
    }
    return {
        "draft": data,
        "sources": [
            {k: v for k, v in s.items() if k in metadata_keys} for s in sources if s["source_id"] in used
        ],
        "source_scope": "Точные цитаты проверены сервером по сохранённым полным текстам. Семантическая проверка здесь ограничена приведёнными цитатами и метаданными, не является независимой проверкой истинности источника.",
    }


class ResearchService:
    def __init__(self, service):
        self.service, self.store, self.settings = service, service.store, service.settings
        self.bank = BankLibrary(service.settings.root)
        with self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS research_jobs(
              id TEXT PRIMARY KEY,tenant TEXT NOT NULL,company_id TEXT NOT NULL,
              idem TEXT NOT NULL,payload_hash TEXT NOT NULL,input_json TEXT NOT NULL,
              state_json TEXT NOT NULL,result_json TEXT,status TEXT NOT NULL,version INTEGER NOT NULL,
              created_at TEXT NOT NULL,lease_until REAL,favorite INTEGER NOT NULL DEFAULT 0,
              UNIQUE(tenant,idem));
            CREATE INDEX IF NOT EXISTS research_queue ON research_jobs(status,created_at);
            CREATE TABLE IF NOT EXISTS research_reviews(run_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,
              verdict TEXT NOT NULL,comment TEXT NOT NULL,reviewed_by TEXT NOT NULL,
              reviewed_at TEXT NOT NULL,result_hash TEXT NOT NULL);
            """)

    def create(self, request, idem):
        self.service.authorize_company(request.company_id)
        raw = request.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        with self.store.connection() as db:
            old = db.execute(
                "SELECT id,payload_hash FROM research_jobs WHERE tenant=? AND idem=?",
                (self.service.tenant, idem),
            ).fetchone()
        if old:
            if old["payload_hash"] != digest:
                raise Conflict("idempotency_payload_mismatch")
            return self.get(old["id"])
        with self.store.connection() as db:
            pending = db.execute(
                "SELECT count(*) FROM research_jobs WHERE tenant=? AND status IN ('QUEUED','RUNNING','WAITING_INPUT')",
                (self.service.tenant,),
            ).fetchone()[0]
            total = db.execute(
                "SELECT count(*) FROM research_jobs WHERE status IN ('QUEUED','RUNNING')"
            ).fetchone()[0]
        if pending >= 3 or total >= 20:
            raise Conflict("research_queue_limit")
        if not request.parent_id and any(
            f["status"] in {"QUEUED", "RUNNING", "DEFERRED_BUDGET"}
            for f in self.service.workspace.files(request.company_id)
        ):
            raise Conflict("company_documents_processing")
        if any(not u.startswith("https://") or len(u) > 1500 for u in request.source_urls):
            raise ValueError("public_https_required")
        parent = self.get(request.parent_id) if request.parent_id else None
        if parent and parent["company_id"] != request.company_id:
            raise Conflict("follow_up_company_mismatch")
        if parent:
            previous_request = self._row(request.parent_id)["input"]
            snapshot = self.service.workspace.load_snapshot(
                previous_request["snapshot_id"], request.company_id
            )
            snapshot["snapshot_id"] = previous_request["snapshot_id"]
        else:
            snapshot = self.service.workspace.snapshot(request.company_id, self.service.corpus, persist=True)
        company_name = next(
            (f.get("text_value") for f in snapshot["profile"]["features"] if f["name"] == "company_name"),
            request.company_id,
        )
        if request.bank_document_id and request.bank_document_id not in {
            d["document_id"] for d in self.bank.documents
        }:
            raise ValueError("unknown_bank_document")
        prefs = self.service.personalization.get()
        raw["job_title"] = request.job_title or prefs["job_title"]
        raw["role"] = request.role or (role_for(request.job_title) if request.job_title else prefs["role"])
        raw.update(
            snapshot_id=snapshot["snapshot_id"],
            as_of=utcnow().isoformat(),
            company_name=company_name,
            bank_company=is_bank_company(company_name, snapshot),
            fresh_news_requested=fresh_news_intent(request.question),
        )
        if parent and parent.get("result"):
            raw["previous_summary"] = parent["result"]["summary"]
        sources = []
        if parent:
            sources = [{**s, "provided_for_current_question": False} for s in parent["sources"]]
        if request.news_item_id:
            item = self.service.news.item(request.company_id, request.news_item_id)
            item_source = news_source(item)
            if item_source["kind"] == "search_lead":
                item_source["kind"] = "url_to_read"
            item_source["provided_for_current_question"] = True
            sources = [s for s in sources if s["source_id"] != item_source["source_id"]]
            sources.append(item_source)
        for url in request.source_urls:
            sources.append(
                {
                    "source_id": new_id("input_url"),
                    "kind": "url_to_read",
                    "url": url,
                    "title": "Ссылка пользователя",
                    "text": "",
                    "provided_for_current_question": True,
                }
            )
        website = snapshot.get("context", {}).get("website", "")
        if website and website not in request.source_urls:
            if not any(s.get("url") == website for s in sources):
                sources.append(
                    {
                        "source_id": new_id("company_website"),
                        "kind": "url_to_read",
                        "url": website,
                        "title": "Сайт, указанный компанией",
                        "text": "",
                    }
                )
        if request.news_text.strip():
            sid = (
                new_id("input_news") if any(s["source_id"] == "input_news" for s in sources) else "input_news"
            )
            sources.append(
                {
                    "source_id": sid,
                    "kind": "user_news",
                    "title": "Новость, предоставленная пользователем",
                    "publisher": "Пользователь",
                    "published_at": None,
                    "text": request.news_text,
                    "review_status": "user_supplied_unverified",
                    "provided_for_current_question": True,
                    "limitations": "Первоисточник и дата события не подтверждены независимо.",
                }
            )
        if len(sources) > 64:
            raise Conflict("research_context_limit")
        state = {
            "phase": "PLAN",
            "llm_calls": 0,
            "tool_calls": 0,
            "replans": 0,
            "sources": sources,
            "observations": [],
            "issues": [],
            "questions": [],
            "error_code": None,
            "next_eligible_at": None,
            "used_tools": [],
            "verification": None,
        }
        run_id = new_id("research")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT id,payload_hash FROM research_jobs WHERE tenant=? AND idem=?",
                (self.service.tenant, idem),
            ).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise Conflict("idempotency_payload_mismatch")
                return self.get(old["id"])
            self.service.check_paid_admission(db, self.service.tenant)
            db.execute(
                "INSERT INTO research_jobs(id,tenant,company_id,idem,payload_hash,input_json,state_json,status,version,created_at) VALUES(?,?,?,?,?,?,?,'QUEUED',1,?)",
                (
                    run_id,
                    self.service.tenant,
                    request.company_id,
                    idem,
                    digest,
                    json.dumps(raw, ensure_ascii=False),
                    json.dumps(state, ensure_ascii=False),
                    utcnow().isoformat(),
                ),
            )
        self.store.audit(
            run_id, "PLAN", "queued", {"company_id": request.company_id, "parent_id": request.parent_id}
        )
        return self.get(run_id)

    def latest_decision_memo(self, company_id, role, profile_version, context_revision):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,result_json,created_at FROM research_jobs WHERE tenant=? AND company_id=? AND status IN ('PARTIAL','SUCCEEDED') ORDER BY created_at DESC LIMIT 12",
                (self.service.tenant, company_id),
            ).fetchall()
        for row in rows:
            if (utcnow() - datetime.fromisoformat(row["created_at"])).total_seconds() > 86400:
                continue
            result = json.loads(row["result_json"] or "{}")
            if result.get("verification", {}).get("policy_version") != POLICY_VERSION:
                continue
            with self.store.connection() as db:
                review = db.execute(
                    "SELECT verdict FROM research_reviews WHERE run_id=? AND tenant=?",
                    (row["id"], self.service.tenant),
                ).fetchone()
            if review and review[0] == "rejected":
                continue
            narrative = result.get("narrative") or {}
            if (
                narrative.get("role") != role
                or narrative.get("context_revision") != context_revision
                or result.get("profile_version") != profile_version
            ):
                continue
            return {
                "run_id": row["id"],
                "title": result["title"],
                "summary": result["summary"],
                "as_of": result["as_of"],
                "forecast_status": narrative["forecast_status"],
                "forecast_limit": narrative["forecast_limit"],
                "scenarios": narrative["scenarios"],
                "context_revision": context_revision,
                "scope": "Сценарии последнего исследования для текущей роли и версии профиля; исходные предпосылки проверяются в отчёте.",
            }
        return None

    def _row(self, run_id, *, tenant=None):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM research_jobs WHERE id=? AND tenant=?", (run_id, tenant or self.service.tenant)
            ).fetchone()
        if not row:
            raise NotFound("research_not_found")
        result = dict(row)
        for key in ("input_json", "state_json", "result_json"):
            value = result.pop(key)
            result[key.removesuffix("_json")] = json.loads(value) if value else None
        return result

    def get(self, run_id):
        row = self._row(run_id)
        self.service.authorize_company(row["company_id"])
        state = row["state"]
        with self.store.connection() as db:
            trace = [
                {"at": r["at"], "node": r["node"], "event": r["event"], "data": json.loads(r["data_json"])}
                for r in db.execute("SELECT * FROM audit WHERE run_id=? ORDER BY seq", (run_id,))
            ]
            review = db.execute(
                "SELECT * FROM research_reviews WHERE run_id=? AND tenant=?", (run_id, self.service.tenant)
            ).fetchone()
        current_rules = (state.get("verification") or {}).get("policy_version") == POLICY_VERSION
        review = dict(review) if review else None
        digest = (
            hashlib.sha256(json.dumps(row["result"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if row["result"]
            else None
        )
        if review and review["result_hash"] != digest:
            review = None
        return {
            "run_id": row["id"],
            "company_id": row["company_id"],
            "company_name": row["input"]["company_name"],
            "question": row["input"]["question"],
            "job_title": row["input"]["job_title"],
            "status": row["status"],
            "version": row["version"],
            "created_at": row["created_at"],
            "favorite": bool(row["favorite"]),
            "parent_id": row["input"].get("parent_id"),
            "result": row["result"],
            "sources": state["sources"],
            "trace": trace,
            "user_review": review,
            "quality_gate": {
                "current_rules": current_rules,
                "status": "not_generated"
                if not row["result"]
                else "legacy_requires_regeneration"
                if not current_rules
                else "reviewed_by_user"
                if review and review["verdict"] == "accepted"
                else "rejected_by_user"
                if review
                else "draft_requires_review",
                "independent_expert_verified": False,
                "scope": "Автоматическая проверка и просмотр пользователем различаются; финансовая экспертиза не подтверждена.",
            },
            **{
                k: state.get(k)
                for k in (
                    "phase",
                    "llm_calls",
                    "tool_calls",
                    "replans",
                    "questions",
                    "error_code",
                    "next_eligible_at",
                    "verification",
                )
            },
        }

    def review(self, run_id, verdict, comment):
        from .identity import current_principal

        job = self.get(run_id)
        principal = current_principal.get()
        if not principal or not job["result"] or not job["quality_gate"]["current_rules"]:
            raise Conflict("current_verified_result_and_account_required")
        if not (job.get("verification") or {}).get("passed"):
            raise Conflict("verified_result_not_ready")
        digest = hashlib.sha256(
            json.dumps(job["result"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO research_reviews VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET verdict=excluded.verdict,comment=excluded.comment,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,result_hash=excluded.result_hash",
                (
                    run_id,
                    self.service.tenant,
                    verdict,
                    comment,
                    principal.user_id,
                    utcnow().isoformat(),
                    digest,
                ),
            )
            self.service.accounts.event(db, principal.user_id, "research_user_" + verdict, job["company_id"])
        return self.get(run_id)["quality_gate"]

    def history(self, company_id):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            return [
                {
                    "run_id": r["id"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "favorite": bool(r["favorite"]),
                    "question": json.loads(r["input_json"])["question"],
                }
                for r in db.execute(
                    "SELECT * FROM research_jobs WHERE company_id=? AND tenant=? ORDER BY created_at DESC LIMIT 25",
                    (company_id, self.service.tenant),
                )
            ]

    def set_favorite(self, run_id, value):
        self.get(run_id)
        with self.store.connection() as db:
            db.execute(
                "UPDATE research_jobs SET favorite=? WHERE id=? AND tenant=?",
                (int(value), run_id, self.service.tenant),
            )

    def answer(self, run_id, payload):
        row = self._row(run_id)
        if row["status"] != "WAITING_INPUT" or row["version"] != payload.expected_version:
            raise Conflict("answer_state_changed")
        state = row["state"]
        state["sources"].append(
            {
                "source_id": new_id("user_answer"),
                "kind": "company_evidence",
                "publisher": "Пользователь компании",
                "title": "Ответ на уточнение",
                "published_at": utcnow().date().isoformat(),
                "text": payload.answer,
                "review_status": "self_reported",
            }
        )
        state.update(phase="PLAN", questions=[])
        self._save(row, state, status="QUEUED")
        self.store.audit(run_id, "PLAN", "user_answer_received", {"characters": len(payload.answer)})
        return self.get(run_id)

    def cancel(self, run_id):
        row = self._row(run_id)
        if row["status"] in {"QUEUED", "RUNNING", "WAITING_INPUT", "DEFERRED_BUDGET"}:
            self._save(row, row["state"], status="CANCELLED")
        return self.get(run_id)

    def _save(self, row, state, *, status="RUNNING", result=None):
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE research_jobs SET status=?,state_json=?,result_json=COALESCE(?,result_json),version=version+1,lease_until=? WHERE id=? AND version=? AND status NOT IN ('CANCELLED','SUCCEEDED','PARTIAL')",
                (
                    status,
                    json.dumps(state, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    utcnow().timestamp() + self.settings.lease_seconds,
                    row["id"],
                    row["version"],
                ),
            )
            if changed.rowcount != 1:
                raise Conflict("research_state_changed")
            db.execute(
                "UPDATE attempts SET status='consumed' WHERE run_id=? AND status='received'", (row["id"],)
            )
        row["version"] += 1
        row["status"], row["state"] = status, state

    def claim(self):
        now = utcnow()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed paid request is not silently replayed.
            for row in db.execute(
                "SELECT * FROM research_jobs WHERE status='RUNNING' AND lease_until<?", (now.timestamp(),)
            ).fetchall():
                state = json.loads(row["state_json"])
                state.update(error_code="interrupted_manual_retry", phase="FINISHED")
                db.execute(
                    "UPDATE research_jobs SET status='FAILED',state_json=?,version=version+1 WHERE id=?",
                    (json.dumps(state), row["id"]),
                )
            for row in db.execute("SELECT * FROM research_jobs WHERE status='DEFERRED_BUDGET'").fetchall():
                state = json.loads(row["state_json"])
                if state.get("next_eligible_at") and datetime.fromisoformat(state["next_eligible_at"]) <= now:
                    db.execute("UPDATE research_jobs SET status='QUEUED' WHERE id=?", (row["id"],))
            if any(
                db.execute("SELECT 1 FROM " + table + " WHERE status='RUNNING' LIMIT 1").fetchone()
                for table in ("runs", "document_jobs", "research_jobs")
            ):
                return None
            row = db.execute(
                "SELECT * FROM research_jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE research_jobs SET status='RUNNING',lease_until=?,version=version+1 WHERE id=?",
                (now.timestamp() + self.settings.lease_seconds, row["id"]),
            )
            run_id, tenant = row["id"], row["tenant"]
        return self._row(run_id, tenant=tenant)


class ResearchEngine:
    def __init__(self, research, gateway):
        self.research, self.gateway = research, gateway
        self.service, self.store, self.settings = research.service, research.store, research.settings

    async def once(self):
        row = self.research.claim()
        if row:
            await self.execute(row)
            return row["id"]
        return None

    async def execute(self, row):
        try:
            async with asyncio.timeout(self.settings.active_seconds):
                await self._execute(row)
        except TimeoutError:
            current = self.research._row(row["id"], tenant=row["tenant"])
            if current["status"] == "RUNNING":
                state = current["state"]
                state.update(phase="FINISHED", error_code="research_limit")
                self.research._save(current, state, status="FAILED")
                self.store.audit(row["id"], "FINISHED", "stopped", {"error_code": "research_limit"})

    async def _execute(self, row):
        state, request = row["state"], row["input"]
        start = time.monotonic()
        snapshot = self.service.workspace.load_snapshot(request["snapshot_id"], row["company_id"])

        def audit(event, data):
            self.store.audit(row["id"], state["phase"], event, data)

        def save(**kw):
            self.research._save(row, state, **kw)

        async def invoke(stage, cls, payload, output_schema=None):
            repair = None
            for attempt in range(2):
                if (
                    state["llm_calls"] >= self.settings.max_calls
                    or time.monotonic() - start > self.settings.active_seconds
                ):
                    raise GatewayError("research_limit")
                state["llm_calls"] += 1
                save()
                audit(
                    "llm_started",
                    {"stage": stage, "repair": bool(attempt), "model": self.settings.research_model},
                )
                raw = await self.gateway.complete(
                    row["id"], stage, payload, output_schema or cls.model_json_schema(), repair=repair
                )
                try:
                    output = cls.model_validate_json(raw)
                    audit("llm_validated", {"stage": stage})
                    return output
                except ValidationError as error:
                    repair = {
                        "raw": raw,
                        "errors": error.errors(include_input=False, include_context=False, include_url=False),
                    }
                    audit(
                        "json_repair",
                        {
                            "stage": stage,
                            "errors": [
                                {"type": e["type"], "location": list(e["loc"]), "message": e["msg"]}
                                for e in repair["errors"]
                            ],
                        },
                    )
            raise GatewayError("model_contract_invalid")

        def add_sources(items):
            by_id = {s["source_id"]: s for s in state["sources"]}
            for item in items:
                item.setdefault("source_id", new_id("source"))
                item.setdefault("retrieved_at", utcnow().isoformat())
                by_id[item["source_id"]] = item
            if len(by_id) > 64:
                raise GatewayError("research_context_limit")
            state["sources"] = list(by_id.values())

        def model_context():
            sources, budget = [], 20000
            wanted = terms(request["question"])
            ranked_sources = sorted(
                state["sources"],
                key=lambda s: (
                    bool(s.get("provided_for_current_question")),
                    len(terms(s.get("text", "")) & wanted),
                ),
                reverse=True,
            )
            for source in ranked_sources:
                item = dict(source)
                cap = {"public_page": 7000, "user_news": 10000}.get(item["kind"], 2000)
                item["text"] = item.get("text", "")[: min(cap, max(0, budget))]
                budget -= len(item["text"])
                sources.append(item)
            features = [
                {**f, "text_value": f.get("text_value", "")[:700] if f.get("text_value") else None}
                for f in snapshot["profile"]["features"][:12]
            ]
            return {
                "question": request["question"],
                "allow_partial": request.get("allow_partial", True),
                "previous_summary": request.get("previous_summary"),
                "company": {
                    "name": request["company_name"],
                    "declared_bank": request["bank_company"],
                    "identity": "Название и роль сообщены пользователем; независимая идентификация не выполнена.",
                    "features": features,
                    "confirmed_context": {
                        "revision": snapshot.get("context", {}).get("revision", 0),
                        "synthetic": snapshot.get("context", {}).get("synthetic", False),
                        "description": snapshot.get("context", {}).get("description", "")[:1200],
                        "fields": {
                            k: {"label": v["label"], "category": v["category"], "value": v["value"][:700]}
                            for k, v in list(snapshot.get("context", {}).get("fields", {}).items())[:12]
                        },
                    },
                },
                "job_title": request["job_title"],
                "role": request.get("role") or role_for(request["job_title"]),
                "selected_news_source_id": request.get("news_item_id"),
                "fresh_news_requested": request.get("fresh_news_requested", False),
                "as_of": request["as_of"],
                "public_search_enabled": request["public_search"],
                "sources": sources,
                "observations": state["observations"][-6:],
                "verification_issues": state["issues"],
                "used_tools": state["used_tools"],
                "remaining_tools": self.settings.max_tools - state["tool_calls"],
                "bank_scope": "Материалы кейса разных дат. OCR не равен полной ручной разметке. Отсутствие свежего обзора не заменяется знаниями модели.",
            }

        async def tool(action):
            if state["tool_calls"] >= self.settings.max_tools:
                return
            state["phase"] = "SOURCES"
            state["tool_calls"] += 1
            save()
            count, error = 0, None
            try:
                if action.tool == "company":
                    docs = {d["document_id"]: d for d in snapshot["documents"]}
                    wanted = terms(request["question"] + " " + action.query)
                    evidence = sorted(
                        snapshot["evidence"], key=lambda e: len(terms(e["quote"]) & wanted), reverse=True
                    )[:5]
                    important = [e for e in snapshot["evidence"] if "_context_" in e["evidence_id"]][:8]
                    evidence = list({e["evidence_id"]: e for e in [*important, *evidence]}.values())[:12]
                    items = []
                    for e in evidence:
                        d = docs[e["document_id"]]
                        items.append(
                            {
                                "source_id": e["evidence_id"],
                                "kind": "company_evidence",
                                "title": d["title"],
                                "publisher": d["publisher"],
                                "published_at": d.get("published_at"),
                                "page": e.get("page"),
                                "locator": e.get("locator"),
                                "text": (
                                    e["quote"]
                                    if len(e["quote"]) >= 35
                                    else "Сведения компании, сообщённые пользователем. "
                                    + e.get("locator", "")
                                    + ": "
                                    + e["quote"]
                                )[:3000],
                                "review_status": "self_reported_or_case_data",
                                "document_id": d["document_id"],
                                "context_key": next(
                                    (
                                        key
                                        for key in snapshot.get("context", {}).get("fields", {})
                                        if "_context_" + key + "_" in e["evidence_id"]
                                    ),
                                    None,
                                ),
                                "limitations": "Из источников текущей компании. Юридическая идентичность и сведения клиента независимо не подтверждены.",
                            }
                        )
                    passages = []
                    for did, chunks in snapshot.get("document_chunks", {}).items():
                        for chunk in chunks:
                            # Retrieval is over frozen raw text, including fields outside the profile schema.
                            for offset in range(0, len(chunk["text"]), 1400):
                                excerpt = chunk["text"][offset : offset + 1800]
                                score = len(terms(excerpt) & wanted)
                                passages.append((score, did, chunk, offset, excerpt))
                    passages.sort(key=lambda x: x[0], reverse=True)
                    for _, did, chunk, offset, text in passages[:3]:
                        d = docs[did]
                        items.append(
                            {
                                "source_id": did + "_" + chunk["id"] + "_" + str(offset),
                                "kind": "company_evidence",
                                "title": d["title"],
                                "publisher": d["publisher"],
                                "published_at": d.get("published_at"),
                                "page": chunk.get("page"),
                                "locator": chunk["locator"],
                                "text": text,
                                "review_status": "self_reported_document",
                                "document_id": did,
                            }
                        )
                    add_sources(items)
                    count = len(items)
                elif action.tool == "bank":
                    items = self.research.bank.search(
                        (action.query or request["question"]) + " " + TOPICS[action.topic],
                        document_id=request.get("bank_document_id"),
                    )
                    if action.topic == "rates" and request.get("bank_document_id") in {None, "bank_case_04"}:
                        for claim in self.service.corpus.claims.values():
                            document = self.service.corpus.documents[claim.document_id]
                            quote = self.service.corpus.evidence[claim.evidence_ids[0]].quote
                            items.append(
                                {
                                    "source_id": claim.claim_id,
                                    "kind": "bank_claim",
                                    "title": document.title,
                                    "publisher": document.publisher,
                                    "published_at": str(document.published_at)
                                    if document.published_at
                                    else None,
                                    "publication_date_raw": document.publication_date_raw,
                                    "url": document.public_url,
                                    "page": 1,
                                    "text": quote,
                                    "claim_type": claim.claim_type.value,
                                    "review_status": "pending_human_review",
                                    "claim_owner": claim.owner.name,
                                    "forecast_horizon": claim.time_scope.model_dump(mode="json"),
                                }
                            )
                    add_sources(items)
                    count = len(items)
                elif action.tool == "public":
                    if not request["public_search"]:
                        raise PublicSourceError("public_search_disabled")
                    items = await self.service.news.for_research(
                        row["company_id"],
                        row["tenant"],
                        snapshot,
                        request.get("role") or role_for(request["job_title"]),
                        action.topic,
                        strict_topic=bool(request.get("news_item_id")),
                    )
                    for item in items:
                        item["provided_for_current_question"] = True
                    add_sources(items)
                    count = len(items)
                elif action.tool == "read_source":
                    source = next((s for s in state["sources"] if s["source_id"] == action.source_id), None)
                    if source and source["kind"] not in {"url_to_read", "search_lead"}:
                        count = 1
                    elif not source or not request["public_search"]:
                        raise PublicSourceError("source_not_readable")
                    else:
                        item = await asyncio.to_thread(page_source, source["url"])
                        item["source_id"] = source["source_id"]
                        if source.get("news_item"):
                            if not article_matches(source, item):
                                raise PublicSourceError("article_content_mismatch")
                            metadata = article_publication(source, item)
                            item["published_at"] = source.get("published_at")
                            item["publication_date_raw"] = source.get("publication_date_raw")
                            item["date_source"] = source.get("date_source")
                            item.update(metadata)
                            item["news_item"] = True
                        item["provided_for_current_question"] = source.get(
                            "provided_for_current_question", False
                        )
                        item["text"] = item["text"][:10000]
                        item["sha256"] = hashlib.sha256(item["text"].encode()).hexdigest()
                        add_sources([item])
                        count = 1
            except PublicSourceError as failure:
                error = str(failure)
            state["used_tools"].append(action.tool)
            observation = {
                "tool": action.tool,
                "reason": action.reason,
                "sources_added": count,
                "error": error,
            }
            state["observations"].append(observation)
            audit("tool_completed", observation)
            save()

        try:
            for required in ("company", "bank"):
                if required not in state["used_tools"]:
                    await tool(
                        ResearchAction(
                            tool=required,
                            reason="Обязательная проверка контекста компании и материалов банка",
                            query=request["question"][:350],
                            topic=topic_for_question(request["question"]),
                        )
                    )
            for source in list(state["sources"]):
                if source["kind"] == "url_to_read" and state["tool_calls"] < self.settings.max_tools:
                    await tool(
                        ResearchAction(
                            tool="read_source",
                            source_id=source["source_id"],
                            reason="Прочитать ссылку, явно указанную пользователем",
                        )
                    )
            while True:
                state["phase"] = "PLAN"
                plan = await invoke("research_plan", ResearchPlan, model_context())
                audit(
                    "plan", {"rationale": plan.rationale, "actions": [a.model_dump() for a in plan.actions]}
                )
                # Required context is enforced by the host, never left to a single prompt.
                for required in ("company", "bank"):
                    if required not in state["used_tools"]:
                        await tool(
                            ResearchAction(
                                tool=required,
                                reason="Обязательная проверка контекста компании и материалов банка",
                                query=request["question"][:350],
                                topic=topic_for_question(request["question"]),
                            )
                        )
                for action in plan.actions:
                    if action.tool in {"write", "ask_user"}:
                        if (
                            action.tool == "ask_user"
                            and action.questions
                            and state["replans"] < self.settings.max_replans
                        ):
                            available = evidence_ledger(state["sources"], request["question"])
                            if (
                                request.get("allow_partial", True)
                                and "company" in state["used_tools"]
                                and any(
                                    a["kind"] in {"bank_claim", "bank_excerpt", "public_page", "user_news"}
                                    for a in available
                                )
                            ):
                                audit(
                                    "partial_instead_of_missing_data_question",
                                    {
                                        "questions": action.questions,
                                        "reason": "Есть источники для частичного вывода; неизвестные показатели не подставляются.",
                                    },
                                )
                                continue
                            state.update(phase="WAITING_INPUT", questions=action.questions)
                            save(status="WAITING_INPUT")
                            audit("question_created", {"questions": action.questions})
                            return
                        continue
                    if (
                        action.tool in {"company", "bank"}
                        and state["used_tools"].count(action.tool) >= 1
                        and not state["issues"]
                    ):
                        continue
                    await tool(action)
                if (
                    any(a.tool == "public" for a in plan.actions)
                    and state["tool_calls"] < self.settings.max_tools
                    and state["llm_calls"] < self.settings.max_calls - 3
                ):
                    continue
                state["phase"] = "WRITE"
                context = model_context()
                ledger = evidence_ledger(state["sources"], request["question"])
                context["sources"] = [
                    {
                        k: s.get(k)
                        for k in (
                            "source_id",
                            "kind",
                            "title",
                            "published_at",
                            "review_status",
                            "limitations",
                        )
                    }
                    for s in state["sources"]
                ]
                context["evidence_ledger"] = ledger
                context["mechanisms"] = {
                    name: {"focus": r["focus"], "condition": r["condition"]} for name, r in RULES.items()
                }
                selected = await invoke(
                    "research_write",
                    ResearchSelection,
                    context,
                    selection_schema(ledger, request["bank_company"]),
                )
                try:
                    draft = assemble_research(selected, ledger, request, snapshot)
                    narrative_payload = {
                        "question": request["question"],
                        "company": {k: v for k, v in context["company"].items() if k != "features"},
                        "job_title": request["job_title"],
                        "role": request["role"],
                        "as_of": request["as_of"],
                        "analysis_policy": planning_policy(
                            request["as_of"], request.get("planning_days", 30)
                        ),
                        "selected_news_source_id": request.get("news_item_id"),
                        "evidence_ledger": ledger,
                        "source_messages": source_message_options(ledger),
                        "verification_issues": state["issues"],
                        "draft": {
                            "findings": [f.model_dump(exclude={"citations"}) for f in draft.findings],
                            "missing_data": draft.missing_data,
                            "limitations": draft.limitations,
                        },
                    }
                    for revision in range(1 + self.settings.max_replans):
                        selection = await invoke(
                            "research_narrative",
                            NarrativeSelection,
                            narrative_payload,
                            decision_schema(
                                ledger, snapshot.get("context", {}), narrative_payload["analysis_policy"]
                            ),
                        )
                        narrative = bind_source_messages(selection, narrative_payload["source_messages"])
                        # Used context is server-owned metadata, derived from the
                        # actual citations, not from a model's assertion of usage.
                        used = {
                            aid
                            for point in [
                                narrative.main_conclusion,
                                *narrative.situation,
                                *narrative.drivers,
                                *narrative.company_impact,
                                *narrative.risks,
                                *narrative.opportunities,
                                *narrative.recommendations,
                            ]
                            for aid in point.anchor_ids
                        }
                        used.update(aid for scenario in narrative.scenarios for aid in scenario.anchor_ids)
                        narrative.context_keys = sorted(
                            key
                            for key in snapshot.get("context", {}).get("fields", {})
                            if any(
                                a["anchor_id"] in used
                                and (a.get("context_key") == key or "_context_" + key + "_" in a["source_id"])
                                for a in ledger
                            )
                        )[:8]
                        issues = validate_narrative(
                            narrative,
                            ledger,
                            snapshot.get("context", {}),
                            request["bank_company"],
                            request.get("news_item_id"),
                            as_of=request["as_of"],
                            planning_days=request.get("planning_days", 30),
                            profile=snapshot["profile"],
                        )
                        if not issues and request.get("news_item_id"):
                            anchors = {a["anchor_id"]: a for a in ledger}
                            refs = [
                                *narrative.main_conclusion.anchor_ids,
                                *[aid for point in narrative.situation for aid in point.anchor_ids],
                            ]
                            if not any(
                                anchors[aid]["source_id"] == request["news_item_id"]
                                for aid in refs
                                if aid in anchors
                            ):
                                issues.append("narrative_selected_news_missing")
                        audit(
                            "narrative_validation",
                            {
                                "approved": not issues,
                                "issues": issues,
                                "unverified_draft": narrative.model_dump(mode="json") if issues else None,
                                "evidence_ledger": ledger if issues else None,
                            },
                        )
                        if not issues or state["replans"] >= self.settings.max_replans:
                            break
                        state["replans"] += 1
                        narrative_payload.update(
                            previous_unverified_narrative=selection.model_dump(mode="json"),
                            validation_issues=issues,
                            per_point_constraints=revision_details(narrative, ledger),
                        )
                        audit("narrative_revision", {"issues": issues, "number": state["replans"]})
                        save()
                    if not issues:
                        draft.narrative = bind_narrative(
                            narrative,
                            ledger,
                            request.get("role") or role_for(request["job_title"]),
                            snapshot.get("context", {}).get("revision", 0),
                        )
                        draft.title, draft.summary = narrative.title, narrative.main_conclusion.text
                        draft.narrative["planning_policy"] = narrative_payload["analysis_policy"]
                        draft.narrative["financial_scope"] = {
                            "net_company_effect": "not_calculated",
                            "reason": "Нет модели баланса и чувствительности компании; описаны условные каналы влияния.",
                        }
                        issues = validate_draft(draft, state["sources"], bank_company=request["bank_company"])
                except ValueError as failure:
                    draft, issues = None, [str(failure)]
                verdict = None
                if not issues:
                    state["phase"] = "VERIFY"
                    verdict = await invoke(
                        "research_verify",
                        ResearchVerdict,
                        {
                            "task": request["question"],
                            "company_name": request["company_name"],
                            "role": request["role"],
                            "bank_company": request["bank_company"],
                            "as_of": request["as_of"],
                            **semantic_verification_input(draft, state["sources"]),
                        },
                    )
                    if not verdict.approved:
                        issues = verdict.issues or ["semantic_verification_rejected"]
                audit(
                    "verification",
                    {
                        "approved": not issues,
                        "issues": issues,
                        "scope": verdict.scope_note
                        if verdict
                        else "Проверка ссылок, точных цитат, чисел и типов утверждений.",
                    },
                )
                state["verification"] = {
                    "passed": not issues,
                    "quote_and_number_checks": True,
                    "semantic_check": bool(verdict),
                    "human_verified": False,
                    "policy_version": POLICY_VERSION,
                    "scope": verdict.scope_note if verdict else None,
                    "warnings": verdict.issues if verdict and verdict.approved else [],
                }
                if issues:
                    state["issues"] = issues
                    if state["replans"] >= self.settings.max_replans:
                        raise GatewayError("research_verification_failed")
                    state["replans"] += 1
                    audit("replan", {"issues": issues, "number": state["replans"]})
                    save()
                    continue
                result = draft.model_dump(mode="json")
                limitations = result["limitations"]
                limitations.extend(state["verification"]["warnings"])
                limitations.append(
                    "Автоматически проверены ссылки, цитаты, числовые опоры и согласованность вывода. Независимая истинность источников и полная ручная разметка не подтверждены."
                )
                if any(s["kind"] == "bank_excerpt" for s in state["sources"]):
                    limitations.append(
                        "Банковские материалы взяты из датированного набора кейса; это не подтверждение актуальной позиции банка на дату исследования."
                    )
                if any(o.get("error") for o in state["observations"]):
                    limitations.append(
                        "Часть внешних источников недоступна; охват ограничен успешно прочитанными материалами."
                    )
                result.update(
                    as_of=request["as_of"],
                    profile_as_of=snapshot["profile"]["as_of"],
                    profile_version=snapshot["profile"]["version"],
                    synthetic_context=snapshot.get("context", {}).get("synthetic", False),
                    profile_snapshot_id=request["snapshot_id"],
                    company_name=request["company_name"],
                    job_title=request["job_title"],
                    verification=state["verification"],
                )
                state["phase"] = "FINISHED"
                status = (
                    "PARTIAL"
                    if result["missing_data"] or any(o.get("error") for o in state["observations"])
                    else "SUCCEEDED"
                )
                save(status=status, result=result)
                audit("published", {"status": status, "findings": len(draft.findings), "checks_passed": True})
                return
        except GatewayError as error:
            state.update(error_code=error.code, next_eligible_at=error.retry_at, phase="FINISHED")
            save(status="DEFERRED_BUDGET" if error.code == "shared_budget_exhausted" else "FAILED")
            audit("stopped", {"error_code": error.code})
        except Conflict:
            return
        except Exception as error:
            state.update(error_code="research_internal_error", phase="FINISHED")
            save(status="FAILED")
            audit(
                "stopped", {"error_code": "research_internal_error", "exception_type": type(error).__name__}
            )

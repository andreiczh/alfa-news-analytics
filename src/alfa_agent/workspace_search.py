"""Private, bounded retrieval over the available news pool and current company sources.

This is an extractive search result, not a generated financial answer. No user
question or internal text is sent to an external search provider by this module.
"""

from __future__ import annotations

import hashlib
import json
import re

from .news_ranking import company_context, duplicate_story, score_item, topics_for
from .decision_support import news_interpretation
from .settings import utcnow
from .periods import period_intent


STOP_WORDS = "что какие какой какая как почему зачем если когда нужно нужны может могут наша нашей нашего компанию компании бизнес бизнеса роль руководителя узнать найти покажи расскажи влияние повлияет повлиять cfo ceo coo cmo cco".split()


def tokens(text):
    return {
        word[:5] if len(word) > 5 else word[:4]
        for word in re.findall(r"[а-яa-z0-9]+", text.casefold().replace("ё", "е"))
        if len(word) >= 3 or word in {"цб", "рф", "ес", "fx", "ai", "ии"}
    }


STOP = tokens(" ".join(STOP_WORDS))


def query_terms(query):
    return tokens(query) - STOP


def excerpt(text, wanted, length=650):
    """Return an exact slice of the stored source, around the first matched word."""
    start = next(
        (
            max(0, m.start() - 100)
            for m in re.finditer(r"[а-яА-ЯёЁa-zA-Z0-9]+", text)
            if tokens(m.group()) & wanted
        ),
        0,
    )
    return text[start : start + length].strip()


def query_topics(query):
    result = set(topics_for({"title": query, "excerpt": ""}))
    for pattern, topic in [
        (r"ставк|процентн|\balm\b", "rates"),
        (r"фондир|ликвид|депозит", "funding"),
        (r"марж|прибыл|убыт", "earnings"),
        (r"конкурент|партн[её]р", "strategy"),
    ]:
        if re.search(pattern, query, re.I):
            result.add(topic)
    return result


class WorkspaceSearch:
    def __init__(self, service):
        self.service = service

    def search(self, company_id, query, *, scope="all", days=7):
        self.service.authorize_company(company_id)
        query = query.strip()
        wanted = query_terms(query)
        snapshot = self.service.workspace.snapshot(company_id, self.service.corpus)
        role = self.service.personalization.get()["role"]
        context = company_context(snapshot)
        context["anonymous"] = self.service.workspace.case.exists(company_id)
        news, company, bank = [], [], []
        latest = self.service.news._latest(self.service.tenant, company_id)
        good = self.service.news._latest(self.service.tenant, company_id, success_only=True)
        if wanted and scope in {"all", "news"} and good:
            raw = json.loads(good["result_json"])
            topics = query_topics(query)
            candidates = []
            for item in raw.get("items", [])[:500]:
                title_score = len(tokens(item["title"]) & wanted)
                text = item.get("excerpt", "") + " " + item.get("text", "")[:10000]
                body_score = len(tokens(text) & wanted)
                topic_score = len(topics & set(topics_for(item)))
                if not title_score and not body_score and not topic_score:
                    continue
                scored, reason = score_item(item, context, role, utcnow(), days)
                if scored:
                    scored["decision"] = news_interpretation(scored, context, role)
                elif reason in {
                    "no_headline_business_channel",
                    "not_company_or_exposure",
                    "no_business_channel",
                    "low_role_priority",
                }:
                    # An explicit question may be broader than the normal role feed.
                    # Keep hard date/entity guards and do not invent a company link.
                    scored = {
                        **item,
                        "priority": "background",
                        "company_relation": "query_only",
                        "decision": {
                            "why": "Совпадает с вашим запросом. Связь события с компанией пока не подтверждена.",
                            "impact": "Чтобы оценить влияние, нужно установить связь с договорами, рынками или приоритетами компании.",
                            "condition": "Совпадение темы не подтверждает финансовую экспозицию.",
                        },
                    }
                else:
                    continue
                candidates.append((title_score * 4 + body_score + topic_score * 2, scored))
            candidates.sort(
                key=lambda pair: (
                    pair[0],
                    bool(pair[1].get("critical_candidate")),
                    pair[1].get("score", 0),
                    pair[1]["published_at"],
                ),
                reverse=True,
            )
            saved = self.service.news.saved_ids(company_id)
            for _, item in candidates:
                if any(duplicate_story(item, previous) for previous in news):
                    continue
                news.append(
                    {**{k: v for k, v in item.items() if k != "text"}, "saved": item["news_id"] in saved}
                )
                if len(news) == 10:
                    break
        if wanted and scope in {"all", "company"}:
            company = self._company(snapshot, wanted)
        if wanted and scope == "all":
            for source in self.service.research.bank.search(query, limit=10):
                if tokens(source["text"]) & wanted:
                    bank.append(
                        {
                            **{k: v for k, v in source.items() if k != "text"},
                            "quote": excerpt(source["text"], wanted),
                        }
                    )
                if len(bank) == 4:
                    break
        files = self.service.workspace.files(company_id)
        processing = sum(f["status"] in {"QUEUED", "RUNNING", "DEFERRED_BUDGET"} for f in files)
        return {
            "query": query,
            "scope": scope,
            "days": days,
            "period_intent": period_intent(query, days),
            "company_id": company_id,
            "role": role,
            "as_of": utcnow().isoformat(),
            "news": news,
            "company": company,
            "bank": bank,
            "news_checked_at": good["finished_at"] if good else None,
            "news_status": latest["status"] if latest else "EMPTY",
            "processing_documents": processing,
            "empty_query_terms": not wanted,
            "summary": f"Показано: {len(news)} новостей, {len(company)} фрагментов компании, {len(bank)} материалов банка.",
            "scope_note": "Подборка по доступным источникам; не полный поиск по интернету. Фрагменты компании — из текущего контекста и обработанных включённых файлов. Для связного вывода запустите исследование.",
        }

    def _company(self, snapshot, wanted):
        candidates = []
        docs = {d["document_id"]: d for d in snapshot["documents"]}
        for did, text in snapshot.get("document_text", {}).items():
            chunks = snapshot.get("document_chunks", {}).get(did) or [
                {"id": "text", "text": text, "locator": "Текст документа"}
            ]
            document = docs[did]
            for chunk in chunks:
                for offset in range(0, len(chunk["text"]), 1600):
                    text = chunk["text"][offset : offset + 1800]
                    score = len(tokens(text) & wanted)
                    if not score:
                        continue
                    quote = excerpt(text, wanted)
                    candidates.append(
                        (
                            score,
                            {
                                "source_id": did + "_" + str(chunk["id"]) + "_" + str(offset),
                                "document_id": did,
                                "kind": "company_document",
                                "title": document["title"],
                                "publisher": document["publisher"],
                                "published_at": document.get("published_at"),
                                "quote": quote,
                                "sha256": hashlib.sha256(quote.encode()).hexdigest(),
                                "page": chunk.get("page"),
                                "locator": chunk.get("locator", "Текст документа"),
                            },
                        )
                    )
        context = snapshot.get("context", {})
        entries = [(key, value["label"], value["value"]) for key, value in context.get("fields", {}).items()]
        if context.get("description"):
            entries.append(("description", "Описание компании", context["description"]))
        for key, label, value in entries:
            score = len(tokens(label + " " + value) & wanted)
            if score:
                candidates.append(
                    (
                        score,
                        {
                            "source_id": "context_" + key,
                            "kind": "company_context",
                            "title": label,
                            "publisher": "Сведения, подтверждённые пользователем",
                            "published_at": None,
                            "quote": excerpt(value, wanted),
                            "locator": "Профиль компании",
                        },
                    )
                )
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        result, per_document = [], {}
        for _, item in candidates:
            key = item.get("document_id", item["source_id"])
            if per_document.get(key, 0) >= 2:
                continue
            per_document[key] = per_document.get(key, 0) + 1
            result.append(item)
            if len(result) == 10:
                break
        return result

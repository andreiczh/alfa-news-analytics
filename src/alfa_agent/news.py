"""Tenant-scoped news jobs, public-only cache and a bounded non-LLM collector."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime

from .news_sources import FALLBACK_FEED, article_matches, article_publication, discovery_sources, parse_feed
from .news_ranking import company_context, rank_news
from .public_sources import PublicSourceError, page_source, read_public
from .identity import role_for
from .settings import utcnow
from .storage import Conflict, NotFound, new_id

REFRESH_SECONDS = 15 * 60
MAX_ARTICLES = 10
MAX_COLLECTION_SECONDS = 85


def news_source(item):
    """A server-read article, or a lead that must still be read by the agent."""
    return {
        "source_id": item["news_id"],
        "kind": "public_page" if item["read_status"] == "article_read" else "search_lead",
        "title": item["title"],
        "publisher": item["publisher"],
        "url": item["url"],
        "published_at": item.get("published_at"),
        "publication_date_raw": item.get("publication_date_raw"),
        "date_source": item.get("date_source"),
        "retrieved_at": item.get("retrieved_at"),
        "text": item.get("text") or item["title"],
        "sha256": item.get("sha256"),
        "review_status": "public_article_read_not_independently_verified"
        if item["read_status"] == "article_read"
        else "headline_only",
        "news_item": True,
        "limitations": "Публикация прочитана; истинность источника и применимость к компании независимо не подтверждены. Дата сохранена из метаданных издателя или поисковой ленты."
        if item["read_status"] == "article_read"
        else "Только поисковая наводка: текст ещё не прочитан, событие не подтверждено.",
    }


class NewsService:
    def __init__(self, service):
        self.service, self.store = service, service.store
        with self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS news_jobs(
              id TEXT PRIMARY KEY,tenant TEXT NOT NULL,company_id TEXT NOT NULL,role TEXT NOT NULL,
              context_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
              finished_at TEXT,lease_until REAL,result_json TEXT,error_code TEXT);
            CREATE INDEX IF NOT EXISTS news_owner ON news_jobs(tenant,company_id,created_at);
            CREATE TABLE IF NOT EXISTS news_public_cache(
              key TEXT PRIMARY KEY,value_json TEXT NOT NULL,expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS news_feedback(
              id TEXT PRIMARY KEY,tenant TEXT NOT NULL,company_id TEXT NOT NULL,news_id TEXT NOT NULL,
              rating TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS news_saved(
              tenant TEXT NOT NULL,company_id TEXT NOT NULL,news_id TEXT NOT NULL,
              item_json TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(tenant,company_id,news_id));
            """)

    def context(self, company_id, persist=False):
        snapshot = self.service.workspace.snapshot(company_id, self.service.corpus, persist=persist)
        snapshot["context"] = self.service.workspace.context(company_id)
        result = company_context(snapshot)
        result["name"] = result["name"] or company_id
        result["anonymous"] |= self.service.workspace.case.exists(company_id)
        return result

    def _latest(self, tenant, company_id, success_only=False):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM news_jobs WHERE tenant=? AND company_id=? "
                + ("AND status='SUCCEEDED' " if success_only else "")
                + "ORDER BY created_at DESC LIMIT 1",
                (tenant, company_id),
            ).fetchone()
        return dict(row) if row else None

    def _enqueue(self, tenant, company_id, context, role, force=False):
        now = utcnow()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE news_jobs SET status='FAILED',error_code='news_refresh_interrupted' WHERE status='RUNNING' AND lease_until<?",
                (now.timestamp(),),
            )
            old = db.execute(
                "SELECT * FROM news_jobs WHERE tenant=? AND company_id=? ORDER BY created_at DESC LIMIT 1",
                (tenant, company_id),
            ).fetchone()
            if old:
                age = (now - datetime.fromisoformat(old["created_at"])).total_seconds()
                if old["status"] == "FAILED":
                    age = (
                        now - datetime.fromisoformat(old["finished_at"] or old["created_at"])
                    ).total_seconds()
                previous_context = json.loads(old["context_json"])
                same_profile = all(
                    context.get(k) == previous_context.get(k)
                    for k in ("name", "industry", "exposure_topics", "website", "context_revision")
                )
                if old["status"] in {"QUEUED", "RUNNING"} or (
                    same_profile and age < (60 if force or old["status"] == "FAILED" else REFRESH_SECONDS)
                ):
                    return old["id"]
            if (
                db.execute("SELECT count(*) FROM news_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[
                    0
                ]
                >= 20
            ):
                raise Conflict("news_queue_limit")
            job_id = new_id("newsjob")
            db.execute(
                "INSERT INTO news_jobs(id,tenant,company_id,role,context_json,status,created_at) VALUES(?,?,?,?,?,'QUEUED',?)",
                (job_id, tenant, company_id, role, json.dumps(context, ensure_ascii=False), now.isoformat()),
            )
        return job_id

    def refresh(self, company_id, role="cfo", force=False):
        self.service.authorize_company(company_id)
        role = role if role in {"cfo", "treasurer", "ceo", "coo", "cco", "cmo", "other"} else role_for(role)
        context = self.context(company_id)
        self._enqueue(self.service.tenant, company_id, context, role, force)
        return self.get(company_id, role=role)

    def get(self, company_id, role="cfo", window_days=7):
        self.service.authorize_company(company_id)
        context = self.context(company_id)
        row = self._latest(self.service.tenant, company_id)
        good = (
            row
            if row and row["status"] == "SUCCEEDED"
            else self._latest(self.service.tenant, company_id, success_only=True)
        )
        result = json.loads(good["result_json"]) if good else {}
        items, excluded = rank_news(result.get("items", []), context, role, utcnow(), window_days)
        # Full article text stays server-side; only short excerpts go to the news cards.
        public = [{k: v for k, v in item.items() if k != "text"} for item in items]
        saved = self.saved_ids(company_id)
        for item in public:
            item["saved"] = item["news_id"] in saved
        checked = good["finished_at"] if good else None
        age = (utcnow() - datetime.fromisoformat(checked)).total_seconds() if checked else None
        previous_context = json.loads(good["context_json"]) if good else {}
        profile_changed = any(
            context.get(k) != previous_context.get(k)
            for k in ("name", "industry", "exposure_topics", "website", "context_revision")
        )
        failed_age = (
            (utcnow() - datetime.fromisoformat(row["finished_at"] or row["created_at"])).total_seconds()
            if row and row["status"] == "FAILED"
            else None
        )
        from .decision_support import overview_brief

        brief = overview_brief(items, context, role)
        brief["last_research"] = self.service.research.latest_decision_memo(
            company_id, role, context.get("profile_version"), context.get("context_revision", 0)
        )
        return {
            "brief": brief,
            "context_revision": context.get("context_revision", 0),
            "status": row["status"] if row else "EMPTY",
            "job_id": row["id"] if row else None,
            "company_id": company_id,
            "role": role,
            "window_days": window_days,
            "items": public,
            "checked_at": checked,
            "as_of": utcnow().isoformat(),
            "refresh_due": failed_age >= 60
            if failed_age is not None
            else age is None or age > REFRESH_SECONDS or profile_changed,
            "retry_after_seconds": max(0, int(60 - failed_age)) if failed_age is not None else 0,
            "stale": bool(age and age > REFRESH_SECONDS),
            "error_code": row.get("error_code") if row else None,
            "sources": result.get("sources", []),
            "candidate_count": len(result.get("items", [])),
            "excluded": excluded,
            "read_count": sum(i["read_status"] == "article_read" for i in public),
            "ranking_version": "news-rules-v2-entity-context",
            "ranking_note": "Баллы — прозрачный приоритет проверки, не вероятность и не размер финансового эффекта.",
            "scope": "Ленты ЦБ/РБК и резервный Интерфакс + поиск по публичному названию. Внутренние показатели и ФИ не передаются в поиск.",
            "profile_note": "Приоритет пересчитан по текущему профилю; доступные источники могли быть собраны по его предыдущей версии.",
        }

    def item(self, company_id, news_id):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT result_json FROM news_jobs WHERE tenant=? AND company_id=? AND status='SUCCEEDED' ORDER BY created_at DESC LIMIT 8",
                (self.service.tenant, company_id),
            ).fetchall()
        for row in rows:
            found = next((i for i in json.loads(row[0])["items"] if i["news_id"] == news_id), None)
            if found:
                return found
        with self.store.connection() as db:
            saved = db.execute(
                "SELECT item_json FROM news_saved WHERE tenant=? AND company_id=? AND news_id=?",
                (self.service.tenant, company_id, news_id),
            ).fetchone()
        if saved:
            return json.loads(saved[0])
        raise NotFound("news_not_accessible")

    def saved_ids(self, company_id):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT news_id FROM news_saved WHERE tenant=? AND company_id=?",
                    (self.service.tenant, company_id),
                )
            }

    def save_item(self, company_id, news_id, saved):
        self.service.authorize_company(company_id)
        item = self.item(company_id, news_id) if saved else None
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not saved:
                db.execute(
                    "DELETE FROM news_saved WHERE tenant=? AND company_id=? AND news_id=?",
                    (self.service.tenant, company_id, news_id),
                )
                return
            existing = db.execute(
                "SELECT 1 FROM news_saved WHERE tenant=? AND company_id=? AND news_id=?",
                (self.service.tenant, company_id, news_id),
            ).fetchone()
            if existing:
                return
            count = db.execute(
                "SELECT count(*) FROM news_saved WHERE tenant=? AND company_id=?",
                (self.service.tenant, company_id),
            ).fetchone()[0]
            if count >= 200:
                raise Conflict("saved_news_limit")
            db.execute(
                "INSERT INTO news_saved VALUES(?,?,?,?,?)",
                (
                    self.service.tenant,
                    company_id,
                    news_id,
                    json.dumps(item, ensure_ascii=False),
                    utcnow().isoformat(),
                ),
            )

    def saved_items(self, company_id, *, limit=25, offset=0):
        self.service.authorize_company(company_id)
        with self.store.connection() as db:
            total = db.execute(
                "SELECT count(*) FROM news_saved WHERE tenant=? AND company_id=?",
                (self.service.tenant, company_id),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT item_json,created_at FROM news_saved WHERE tenant=? AND company_id=? "
                "ORDER BY created_at DESC,news_id LIMIT ? OFFSET ?",
                (self.service.tenant, company_id, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    **{k: v for k, v in json.loads(r[0]).items() if k != "text"},
                    "saved": True,
                    "saved_at": r[1],
                }
                for r in rows
            ],
            "total": total,
            "has_more": offset + len(rows) < total,
        }

    def feedback(self, company_id, news_id, rating):
        self.item(company_id, news_id)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO news_feedback VALUES(?,?,?,?,?,?)",
                (new_id("nf"), self.service.tenant, company_id, news_id, rating, utcnow().isoformat()),
            )

    def _cache_get(self, key):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT value_json FROM news_public_cache WHERE key=? AND expires_at>?", (key, time.time())
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_set(self, key, value, seconds):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO news_public_cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at",
                (key, json.dumps(value, ensure_ascii=False), time.time() + seconds),
            )
            db.execute("DELETE FROM news_public_cache WHERE expires_at<?", (time.time(),))

    async def collect(self, context, role):
        semaphore = asyncio.Semaphore(4)

        async def cached_read(url, article=False):
            key = ("article:" if article else "feed:") + hashlib.sha256(url.encode()).hexdigest()
            old = self._cache_get(key)
            if old:
                if "error" in old:
                    raise PublicSourceError(old["error"])
                return old
            try:
                async with semaphore:
                    value = await asyncio.wait_for(
                        asyncio.to_thread(page_source if article else read_public, url), timeout=28
                    )
                self._cache_set(key, value, 3600 if article else 300)
                return value
            except (PublicSourceError, TimeoutError) as error:
                code = str(error) if isinstance(error, PublicSourceError) else "public_timeout"
                self._cache_set(key, {"error": code}, 90)
                raise PublicSourceError(code) from None

        async def feed(key, label, url):
            try:
                raw = await cached_read(url)
                items = parse_feed(raw["text"], source_id=key, label=label, feed_url=url)
                return items, {
                    "name": label,
                    "url": url.split("?q=")[0] if key == "company_search" else url,
                    "status": "ok",
                    "count": len(items),
                }
            except PublicSourceError as error:
                return [], {"name": label, "status": "unavailable", "error": str(error), "count": 0}

        output = await asyncio.gather(
            *(feed(*s) for s in discovery_sources(context["name"], role, context["anonymous"]))
        )
        if any(status["status"] != "ok" for _, status in output):
            output.append(await feed(*FALLBACK_FEED))
        by_url, health = {}, []
        for entries, status in output:
            health.append(status)
            for item in entries:
                item["retrieved_at"] = utcnow().isoformat()
                previous = by_url.get(item["url"])
                if not previous or (
                    previous["feed_id"] == "company_search" and item["feed_id"] != "company_search"
                ):
                    by_url[item["url"]] = item
        all_items = list(by_url.values())
        # Select a bounded mix across roles; one employee's role does not hide all other topics.
        selected = []
        for candidate_role in dict.fromkeys([role, "cfo", "coo", "cco", "treasurer", "ceo"]):
            ranked, _ = rank_news(all_items, context, candidate_role, utcnow(), 90)
            for item in ranked[:3]:
                if item["news_id"] not in {i["news_id"] for i in selected}:
                    selected.append(by_url[item["url"]])
        selected = selected[:MAX_ARTICLES]

        async def article(item):
            try:
                value = await cached_read(item["url"], article=True)
                if not article_matches(item, value):
                    raise PublicSourceError("article_content_mismatch")
                try:
                    item.update(article_publication(item, value))
                except PublicSourceError:
                    item["read_status"] = "date_conflict"
                    item["read_error"] = "publisher_date_conflict"
                    return
                item.update(
                    text=value["text"][:14000],
                    read_status="article_read",
                    kind="public_page",
                    retrieved_at=utcnow().isoformat(),
                    sha256=hashlib.sha256(value["text"][:14000].encode()).hexdigest(),
                    publisher=value["publisher"],
                    article_url=value["url"],
                )
                # The source excerpt remains a publisher excerpt; it is not an AI summary.
                if not item["excerpt"]:
                    item["excerpt"] = " ".join(value["text"].split())[:300]
            except PublicSourceError as error:
                item["read_error"] = str(error)

        await asyncio.gather(*(article(i) for i in selected))
        return {
            "items": all_items,
            "sources": health,
            "fetched_at": utcnow().isoformat(),
            "article_reads_attempted": len(selected),
            "no_llm_used": True,
        }

    def claim(self):
        now = utcnow()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE news_jobs SET status='FAILED',error_code='news_refresh_interrupted' WHERE status='RUNNING' AND lease_until<?",
                (now.timestamp(),),
            )
            row = db.execute(
                "SELECT * FROM news_jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE news_jobs SET status='RUNNING',lease_until=? WHERE id=? AND status='QUEUED'",
                (now.timestamp() + MAX_COLLECTION_SECONDS + 20, row["id"]),
            )
            return dict(row)

    async def once(self):
        row = self.claim()
        if not row:
            return None
        try:
            result = await asyncio.wait_for(
                self.collect(json.loads(row["context_json"]), row["role"]), MAX_COLLECTION_SECONDS
            )
            ok = any(s["status"] == "ok" for s in result["sources"])
            with self.store.connection() as db:
                db.execute(
                    "UPDATE news_jobs SET status=?,finished_at=?,result_json=?,error_code=?,lease_until=NULL WHERE id=?",
                    (
                        "SUCCEEDED" if ok else "FAILED",
                        utcnow().isoformat(),
                        json.dumps(result, ensure_ascii=False),
                        None if ok else "news_sources_unavailable",
                        row["id"],
                    ),
                )
        except (TimeoutError, PublicSourceError):
            with self.store.connection() as db:
                db.execute(
                    "UPDATE news_jobs SET status='FAILED',finished_at=?,error_code='news_refresh_timeout',lease_until=NULL WHERE id=?",
                    (utcnow().isoformat(), row["id"]),
                )
        except Exception as error:
            logging.getLogger(__name__).error("News refresh stopped (%s)", type(error).__name__)
            with self.store.connection() as db:
                db.execute(
                    "UPDATE news_jobs SET status='FAILED',finished_at=?,error_code='news_refresh_failed',lease_until=NULL WHERE id=?",
                    (utcnow().isoformat(), row["id"]),
                )
        return row["id"]

    async def work_loop(self):
        while True:
            try:
                await self.once()
            except Exception as error:
                logging.getLogger(__name__).error("News queue unavailable (%s)", type(error).__name__)
            await asyncio.sleep(1)

    async def for_research(self, company_id, tenant, snapshot, role, topic, *, strict_topic=False):
        context = company_context({**snapshot, "context": self.service.workspace.context(company_id)})
        context["name"] = context["name"] or company_id
        context["anonymous"] |= self.service.workspace.case.exists(company_id)
        job_id = self._enqueue(tenant, company_id, context, role)
        for _ in range(MAX_COLLECTION_SECONDS + 5):
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT status,result_json FROM news_jobs WHERE id=? AND tenant=?", (job_id, tenant)
                ).fetchone()
            if row["status"] not in {"QUEUED", "RUNNING"}:
                break
            await asyncio.sleep(1)
        if not row or row["status"] != "SUCCEEDED":
            raise PublicSourceError("news_sources_unavailable")
        items, _ = rank_news(json.loads(row["result_json"])["items"], context, role, utcnow(), 7)
        topic_map = {
            "rates": {"rates", "funding"},
            "inflation": {"inflation"},
            "demand": {"demand"},
            "labor": {"inflation", "operations"},
            "commodities": {"inflation", "fx"},
            "competitors": {"products", "strategy"},
            "regulation": {"capital"},
        }
        wanted = topic_map.get(topic, set())
        if strict_topic and wanted:
            items = [i for i in items if wanted & set(i["topics"])]
        items.sort(key=lambda i: bool(wanted & set(i["topics"])), reverse=True)
        return [news_source(i) for i in items[:5]]

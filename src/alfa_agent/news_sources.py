"""Public news discovery. Feeds and headlines are leads until the article is read."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit, quote
from xml.etree import ElementTree

from .public_sources import PublicSourceError, PageText

FEEDS = (
    ("cbr_press", "Пресс-релизы Банка России", "https://www.cbr.ru/rss/RssPress"),
    ("cbr_events", "Новости Банка России", "https://www.cbr.ru/rss/eventrss"),
    ("cbr_updates", "Новое на сайте Банка России", "https://www.cbr.ru/rss/RssNews"),
    ("rbc", "РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
)


def plain(text, limit=1200):
    parser = PageText()
    parser.feed(unescape(text or ""))
    return " ".join(" ".join(parser.parts).split())[:limit]


def clean_news_url(url):
    """Unwrap the observed Bing link; never invent a publisher URL or upgrade HTTP."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        if host in {"bing.com", "www.bing.com"} and parts.path.startswith("/news/apiclick"):
            values = parse_qs(parts.query).get("url", [])
            if len(values) != 1:
                return ""
            parts = urlsplit(values[0])
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return ""
        if parts.port not in {None, 443} or len(url) > 2000:
            return ""
        host = parts.hostname.lower().rstrip(".")
        if host in {"localhost", "metadata.google.internal"} or host.endswith(
            (".localhost", ".local", ".internal")
        ):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        tracking = {"fbclid", "gclid", "yclid", "oc", "from", "ref", "referrer"}
        params = [
            (k, v)
            for k, vals in parse_qs(parts.query, keep_blank_values=True).items()
            for v in vals
            if not k.lower().startswith("utm_") and k.lower() not in tracking
        ]
        return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", urlencode(params), ""))
    except (ValueError, UnicodeError):
        return ""


def publication_time(value):
    """Only a publisher date, never the fetch time, title event date, or current year."""
    if not value:
        return None
    try:
        stamp = parsedate_to_datetime(value.strip())
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if stamp.tzinfo is None:
        # A date-only ISO metadata field is handled as a date, not invented local time.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return None
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        return stamp.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return None


def calendar_date(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except (ValueError, TypeError):
            return None


def article_publication(item, article):
    """Publisher metadata takes precedence over an aggregator, without losing either."""
    value = article.get("published_at")
    if not value:
        return {}
    if item.get("date_source") == "search_feed":
        return {
            "search_published_at": item.get("published_at"),
            "published_at": publication_time(value),
            "publication_date": calendar_date(value),
            "publication_date_raw": value,
            "date_source": "publisher_article",
        }
    feed_day = item.get("publication_date") or calendar_date(item.get("publication_date_raw"))
    if feed_day and calendar_date(value) != feed_day:
        raise PublicSourceError("publisher_date_conflict")
    return {}


def company_queries(name, role):
    """Only a public name and fixed vocabulary may leave the service."""
    if re.search(r"\b[UFG]_\d{3,}\b", name):
        return []
    name = " ".join(re.findall(r"[\wА-Яа-яЁё -]+", name))[:160].strip()
    if len(name) < 2 or name.casefold().startswith("синтетическ"):
        return []
    # Company-only discovery retains weak signals that a role query could miss.
    return [("company_search", '"' + name + '"')]


def discovery_sources(name, role, anonymous=False):
    sources = list(FEEDS)
    if not anonymous:
        for key, query in company_queries(name, role):
            sources.append(
                (
                    key,
                    "Новостной поиск Bing",
                    "https://www.bing.com/news/search?format=rss&setlang=ru-RU&q=" + quote(query),
                )
            )
    return sources


def parse_feed(raw, *, source_id, label, feed_url):
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise PublicSourceError("unsafe_news_xml")
    try:
        root = ElementTree.fromstring(raw.lstrip("\ufeff"))
    except ElementTree.ParseError:
        raise PublicSourceError("news_feed_invalid") from None
    items = []
    nodes = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    for entry in nodes[:150]:

        def field(name):
            for child in entry:
                if child.tag.split("}")[-1].lower() == name:
                    return "".join(child.itertext()).strip()
            return ""

        title = plain(field("title"), 400)
        link = field("link")
        if not link:
            link = next((c.get("href", "") for c in entry if c.tag.endswith("link")), "")
        link = clean_news_url(link)
        if not title or not link:
            continue
        date_raw = field("pubdate") or field("published") or field("updated")
        publisher = field("source") or field("news:source") or label
        items.append(
            {
                "news_id": "news_" + hashlib.sha256(link.encode()).hexdigest()[:24],
                "title": title,
                "url": link,
                "publisher": publisher[:180],
                "published_at": publication_time(date_raw),
                "publication_date": calendar_date(date_raw),
                "publication_date_raw": date_raw or None,
                "date_source": "publisher_feed" if source_id != "company_search" else "search_feed",
                "excerpt": plain(field("description") or field("summary"), 1300),
                "feed_id": source_id,
                "feed_url": feed_url,
                "read_status": "headline_only",
                "text": "",
                "kind": "search_lead",
            }
        )
    return items


def article_matches(item, article):
    def tokens(s):
        return {w[:6] for w in re.findall(r"[а-яёa-z]{4,}", s.casefold())}

    wanted = tokens(item["title"])
    present = tokens(article.get("title", "") + " " + article.get("text", "")[:5000])
    return len(article.get("text", "")) >= 180 and bool(wanted) and len(wanted & present) / len(wanted) >= 0.5

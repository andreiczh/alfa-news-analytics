"""Bounded public HTTPS reads. Requests never contain client documents or a person's name."""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import re
import socket
import ssl
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree


class PublicSourceError(ValueError):
    pass


def public_target(url):
    if len(url) > 1500 or any(ord(c) < 33 for c in url):
        raise PublicSourceError("invalid_public_url")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise PublicSourceError("invalid_public_url") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        raise PublicSourceError("public_https_required")
    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if (
        len(host) > 253
        or host in {"localhost", "metadata.google.internal"}
        or host.endswith((".local", ".localhost", ".internal"))
    ):
        raise PublicSourceError("private_address_blocked")
    try:
        addresses = {r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise PublicSourceError("public_dns_unavailable") from None
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise PublicSourceError("private_address_blocked")
    return host, sorted(addresses)[0], parts


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated address; keep original hostname for TLS and Host.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def read_public(url, *, _depth=0):
    if _depth > 3:
        raise PublicSourceError("redirect_limit")
    host, address, parts = public_target(url)
    connection = _PinnedHTTPS(host, address)
    try:
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += "?" + quote(parts.query, safe="%=&:+,/?-._~")
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "BusinessPulseHackathon/0.3 (public research)",
                "Accept": "text/html,application/rss+xml,application/xml,text/plain",
                "Accept-Encoding": "identity",
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            target = urljoin(url, response.getheader("Location", ""))
            if target == url:
                raise PublicSourceError("redirect_limit")
            return read_public(target, _depth=_depth + 1)
        if response.status != 200:
            raise PublicSourceError("public_page_unavailable")
        kind = response.getheader("Content-Type", "").lower()
        if not any(t in kind for t in ("text/", "xml", "rss")):
            raise PublicSourceError("public_text_only")
        if response.getheader("Content-Encoding", "identity").lower() not in {"identity", "gzip"}:
            raise PublicSourceError("unsupported_public_encoding")
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise PublicSourceError("public_page_size_limit")
        if response.getheader("Content-Encoding", "").lower() == "gzip":
            import io

            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                raw = compressed.read(1_000_001)
            if len(raw) > 1_000_000:
                raise PublicSourceError("public_page_size_limit")
        charset = re.search(r"charset=([\w-]+)", kind)
        encoding = charset.group(1) if charset else "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return {"url": url, "host": host, "content_type": kind, "text": text}
    except PublicSourceError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise PublicSourceError("public_page_unavailable") from None
    finally:
        connection.close()


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title = []
        self.parts = []
        self.main_parts = []
        self.main_depth = 0
        self.published = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"main", "article"}:
            self.main_depth += 1
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "h1", "h2", "h3", "li", "div", "br", "tr"}:
            self.parts.append("\n")
            if self.main_depth:
                self.main_parts.append("\n")
        if tag == "meta" and (attrs.get("property") or attrs.get("name")) in {
            "article:published_time",
            "datePublished",
            "date",
        }:
            value = attrs.get("content", "")
            if re.match(r"^\d{4}-\d{2}-\d{2}", value):
                self.published = value[:10]

    def handle_endtag(self, tag):
        if tag in {"main", "article"}:
            self.main_depth = max(0, self.main_depth - 1)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.skip:
            self.parts.append(data)
            if self.main_depth:
                self.main_parts.append(data)


def page_source(url):
    item = read_public(url)
    parser = PageText()
    parser.feed(item["text"])
    parts = parser.main_parts if len("".join(parser.main_parts)) > 100 else parser.parts
    text = "\n".join(" ".join(line.split()) for line in "".join(parts).splitlines() if line.strip())
    if len(text) < 60:
        raise PublicSourceError("public_text_unavailable")
    return {
        "title": " ".join(parser.title)[:240] or item["host"],
        "url": item["url"],
        "publisher": item["host"],
        "published_at": parser.published,
        "text": text[:45000],
        "kind": "public_page",
        "review_status": "unverified_public_source",
    }


TOPICS = {
    "rates": "ключевая ставка денежно кредитная политика",
    "inflation": "инфляция цены",
    "demand": "спрос потребление экономика",
    "labor": "рынок труда зарплаты",
    "commodities": "сырье цены",
    "competitors": "конкуренты рынок",
    "regulation": "регулирование",
    "company": "финансовые результаты официальный сайт",
}


def search_headlines(company_name, topic):
    """Only public company name + a fixed server topic leave the service."""
    topic = TOPICS.get(topic)
    if topic is None:
        raise PublicSourceError("unsupported_public_topic")
    query = company_name[:160] + " " + topic
    feed = read_public("https://www.bing.com/search?format=rss&q=" + quote(query))
    if "<!DOCTYPE" in feed["text"].upper() or "<!ENTITY" in feed["text"].upper():
        raise PublicSourceError("public_search_unavailable")
    try:
        root = ElementTree.fromstring(feed["text"])
    except ElementTree.ParseError:
        raise PublicSourceError("public_search_unavailable") from None
    items = []
    for node in root.findall(".//item")[:6]:
        title = unescape(node.findtext("title", ""))[:400]
        publisher = node.findtext("source", "")[:160]
        url = node.findtext("link", "")
        if not url.startswith("https://") or not title:
            continue
        items.append(
            {
                "title": title,
                "publisher": publisher or urlsplit(url).hostname or "Поисковый RSS",
                "url": url,
                "published_at": None,
                "publication_date_raw": node.findtext("pubDate"),
                "text": title,
                "kind": "search_lead",
                "review_status": "headline_only",
                "limitations": "Поисковая наводка. Текст публикации не прочитан; заголовок не подтверждает событие.",
            }
        )
    return items

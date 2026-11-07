"""Bounded public HTTP(S) text reader. No cookies, credentials or script execution."""
from __future__ import annotations

from html.parser import HTMLParser
from collections import OrderedDict
from email.message import Message
import http.client
import ipaddress
import json
import hashlib
import socket
import ssl
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

try:
    from .public_dns import PublicDNSError, resolve_fake_ip
except ImportError:
    from public_dns import PublicDNSError, resolve_fake_ip

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 8
MAX_CACHED_SOURCES = 8
HTTP_CHARSETS = {"utf-8": "utf-8", "utf8": "utf-8", "gb18030": "gb18030",
                 "gbk": "gbk", "cp936": "gbk", "gb2312": "gbk"}


class PublicWebError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _ambiguous_charset_parameters(content_type):
    quoted = escaped = False
    start = 0
    names = []
    for index, character in enumerate(content_type):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            names.append(content_type[start:index].split("=", 1)[0].strip().lower())
            start = index + 1
    names.append(content_type[start:].split("=", 1)[0].strip().lower())
    return quoted or escaped or any(name.startswith("charset*") for name in names)


def _response_encoding(response, content_type):
    if content_type.split(";", 1)[0].lower() == "application/json":
        return "utf-8"
    get_all = getattr(getattr(response, "headers", None), "get_all", None)
    if callable(get_all) and len(get_all("Content-Type", []) or []) != 1:
        return "utf-8"
    if _ambiguous_charset_parameters(content_type):
        return "utf-8"
    message = Message()
    try:
        message["Content-Type"] = content_type
        parameters = message.get_params(header="content-type", unquote=True) or []
    except (TypeError, ValueError):
        return "utf-8"
    charsets = [value for name, value in parameters[1:] if name.lower() == "charset"]
    if len(charsets) != 1 or not isinstance(charsets[0], str) or not charsets[0].isascii():
        return "utf-8"
    label = charsets[0].strip(" \t").lower()
    # GB2312 is a GBK label for this bounded HTTP-header compatibility path.
    return HTTP_CHARSETS.get(label, "utf-8") if len(label) <= 64 else "utf-8"


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
        self.links = []
        self._link = None
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag == "a" and not self.hidden:
            href = dict(attrs).get("href")
            if href:
                self._link = {"href": href, "text": ""}
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        elif tag == "a" and self._link is not None:
            if len(self.links) < 300:
                self.links.append(self._link)
            self._link = None
    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())
            if self._link is not None:
                self._link["text"] += data.strip() + " "


def public_target(url: str):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        raise PublicWebError("INVALID_URL")
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise PublicWebError("INVALID_URL")
    host = host.encode("idna").decode("ascii").lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PublicWebError("PRIVATE_TARGET")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise PublicWebError("PORT_NOT_ALLOWED")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise PublicWebError("NO_PUBLIC_ADDRESS")
    ips = [ipaddress.ip_address(address[4][0].split("%", 1)[0]) for address in addresses]
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if not literal and all(isinstance(ip, ipaddress.IPv4Address)
                           and ip in ipaddress.ip_network("198.18.0.0/15") for ip in ips):
        try:
            return parsed, host, port, resolve_fake_ip(host)[0]
        except PublicDNSError:
            raise PublicWebError("PUBLIC_DNS_UNAVAILABLE") from None
    public = []
    for ip in ips:
        check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not check.is_global or check.is_multicast or check.is_reserved or check.is_unspecified:
            raise PublicWebError("PRIVATE_TARGET")
        public.append(str(ip))
    return parsed, host, port, public[0]


def _fetch_source(url: str) -> dict:
    deadline = time.monotonic() + 20
    for _hop in range(4):
        if time.monotonic() >= deadline:
            raise PublicWebError("WEB_TIMEOUT")
        parsed, host, port, address = public_target(url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        # Pin the TCP destination to the already-validated IP. HTTPSConnection
        # still validates TLS and sends SNI for the original hostname. This
        # avoids resolving an attacker-controlled hostname again at connect.
        connection._create_connection = lambda _dest, timeout=TIMEOUT, source_address=None: socket.create_connection((address, port), timeout, source_address)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"User-Agent": "Jingban-Agent/0.1", "Accept": "text/html,text/plain,application/json", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise PublicWebError("INVALID_REDIRECT")
                url = urljoin(url, location)
                continue
            if not 200 <= response.status < 300:
                raise PublicWebError("PROVIDER_RATE_LIMITED" if response.status == 429 else
                                     "WEB_SOURCE_NOT_FOUND" if response.status == 404 else "HTTP_FAILED")
            content_type_header = response.getheader("Content-Type") or "text/plain"
            content_type = content_type_header.lower()
            if not (content_type.startswith("text/") or content_type.split(";", 1)[0] in {"application/json", "application/xml", "application/xhtml+xml"}):
                raise PublicWebError("UNSUPPORTED_CONTENT")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise PublicWebError("UNSUPPORTED_ENCODING")
            chunks = []
            size = 0
            while size <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise PublicWebError("WEB_TIMEOUT")
                chunk = response.read1(min(8192, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            truncated = len(raw) > MAX_BYTES
            text = raw[:MAX_BYTES].decode(_response_encoding(response, content_type_header), errors="replace")
            links = []
            if "html" in content_type:
                parser = TextOnly()
                parser.feed(text)
                text = "\n".join(parser.parts)
                for link in parser.links:
                    absolute = urljoin(url, link["href"])
                    parts = urlsplit(absolute)
                    if parts.scheme in {"http", "https"} and not parts.username and not parts.password:
                        links.append({"url": absolute[:4096], "text": link["text"].strip()[:300]})
            return {"url": url, "text": text, "source_truncated": truncated, "links": links,
                    "source_sha256": hashlib.sha256(raw[:MAX_BYTES]).hexdigest(),
                    "untrusted_external_data": True}
        finally:
            connection.close()
    raise PublicWebError("TOO_MANY_REDIRECTS")


def _validate_page(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
        raise PublicWebError("INVALID_WEB_PAGE")


def _page(source, offset, limit):
    _validate_page(offset, limit)
    text = source["text"]
    end = min(len(text), offset + limit)
    return {**source, "text": text[offset:end], "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "truncated": source["source_truncated"] or end < len(text)}


def fetch_public_text(url: str, offset=0, limit=16000) -> str:
    _validate_page(offset, limit)
    return json.dumps(_page(_fetch_source(url), offset, limit), ensure_ascii=False)


class PublicWebReader:
    """Task-local source snapshots make pagination consistent and inexpensive."""
    def __init__(self, store=None):
        self.store = store
        self.sources = OrderedDict()
        self._index_directory = None
        self._lock = threading.RLock()
        self._closed = False

    def _snapshot_index(self, url, source=None):
        """Disk metadata keeps both large bodies and URL/link indexes bounded in RAM.

        The index is task-private, not an extra table in the conversation store;
        revoking that store can never be bypassed by an old cached reference.
        """
        if self._index_directory is None:
            if source is None:
                return None
            self._index_directory = tempfile.TemporaryDirectory(prefix="aura-web-snapshots-")
        connection = None
        try:
            connection = sqlite3.connect(str(Path(self._index_directory.name) / "index.sqlite3"))
            connection.execute("CREATE TABLE IF NOT EXISTS snapshots (url TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            if source is not None:
                metadata = {key: value for key, value in source.items() if key != "text"}
                with connection:
                    connection.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)",
                                       (url, json.dumps(metadata, ensure_ascii=False)))
                return None
            row = connection.execute("SELECT metadata FROM snapshots WHERE url=?", (url,)).fetchone()
            return json.loads(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError):
            raise PublicWebError("WEB_SNAPSHOT_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    def _stored_page(self, source, offset, limit):
        if self.store is None or not source.get("reference"):
            raise PublicWebError("WEB_SOURCE_EVICTED")
        archived = json.loads(self.store.read(source["reference"], offset=offset, limit=limit))
        end = min(archived["total_characters"], offset + len(archived["text"]))
        return {**{key: value for key, value in source.items() if key not in {"text", "_archive_changed"}},
                "text": archived["text"], "offset": offset, "total_chars": archived["total_characters"],
                "next_offset": end if end < archived["total_characters"] else None,
                "truncated": source["source_truncated"] or end < archived["total_characters"]}

    def close(self):
        with self._lock:
            self._closed = True
            self.sources.clear()
            if self._index_directory is not None:
                self._index_directory.cleanup()
                self._index_directory = None

    def read(self, url, offset=0, limit=16000):
        _validate_page(offset, limit)
        if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
            raise PublicWebError("INVALID_URL")
        with self._lock:
            if self._closed:
                raise PublicWebError("WEB_READER_CLOSED")
            source = self.sources.get(url)
            if source is None:
                source = self._snapshot_index(url)
                if source is not None and self.store is None:
                    raise PublicWebError("WEB_SOURCE_EVICTED")
                if source is None:
                    source = _fetch_source(url)
                    if self.store is not None:
                        reference = self.store.put_text(source["text"], kind="web_source",
                            metadata={"url": source["url"], "source_sha256": source["source_sha256"]})
                        source["reference"] = reference["reference"]
                        # Archive redaction may change length/content. In that
                        # case all pages, including the first, use one canonical
                        # stored snapshot rather than switching text on eviction.
                        source["_archive_changed"] = reference["sha256"] != hashlib.sha256(source["text"].encode()).hexdigest()
                if len(self.sources) >= MAX_CACHED_SOURCES:
                    oldest = next(iter(self.sources))
                    self._snapshot_index(oldest, self.sources[oldest])
                    del self.sources[oldest]
                self.sources[url] = source
            self.sources.move_to_end(url)
            if "text" not in source or source.get("_archive_changed"):
                result = self._stored_page(source, offset, limit)
            else:
                result = _page({key: value for key, value in source.items() if key != "_archive_changed"}, offset, limit)
            return json.dumps(result, ensure_ascii=False)

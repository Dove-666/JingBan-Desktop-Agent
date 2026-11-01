"""Bounded public HTTP(S) text reader. No cookies, credentials or script execution."""
from __future__ import annotations

from html.parser import HTMLParser
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 262144
TIMEOUT = 8


class PublicWebError(Exception):
    pass


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


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
    public = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not check.is_global or check.is_multicast or check.is_reserved or check.is_unspecified:
            raise PublicWebError("PRIVATE_TARGET")
        public.append(str(ip))
    return parsed, host, port, public[0]


def fetch_public_text(url: str) -> str:
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
            connection.request("GET", path, headers={"User-Agent": "AURA-Agent/0.1", "Accept": "text/html,text/plain,application/json", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise PublicWebError("INVALID_REDIRECT")
                url = urljoin(url, location)
                continue
            if not 200 <= response.status < 300:
                raise PublicWebError("HTTP_FAILED")
            content_type = (response.getheader("Content-Type") or "text/plain").lower()
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
            text = raw[:MAX_BYTES].decode("utf-8", errors="replace")
            if "html" in content_type:
                parser = TextOnly()
                parser.feed(text)
                text = "\n".join(parser.parts)
            return json.dumps({"url": url, "text": text[:16000], "truncated": truncated or len(text) > 16000,
                               "untrusted_external_data": True}, ensure_ascii=False)
        finally:
            connection.close()
    raise PublicWebError("TOO_MANY_REDIRECTS")

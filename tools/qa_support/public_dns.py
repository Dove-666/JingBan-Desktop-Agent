"""Fixed TLS-pinned public DNS fallback for system Fake-IP answers only.

This is not a general proxy/resolver setting. Callers still reject private
origins and pin each final TCP destination. No cookies, auth or redirects.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

RESOLVER_HOST = "dns.alidns.com"
RESOLVER_IP = "223.5.5.5"
RESOLVER_PORT = 443
MAX_RESPONSE_BYTES = 65_536
MAX_ANSWERS = 64
RESOLVE_TIMEOUT = 4.0
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class PublicDNSError(ValueError):
    def __init__(self):
        super().__init__("PUBLIC_DNS_UNAVAILABLE")


def canonical_hostname(value):
    if not isinstance(value, str) or not value or len(value) > 254:
        raise PublicDNSError()
    value = value[:-1] if value.endswith(".") else value
    try:
        name = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise PublicDNSError() from None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise PublicDNSError()
    if (not name or len(name) > 253 or "." not in name
            or name.endswith((".localhost", ".local", ".internal"))
            or not all(_LABEL.fullmatch(part) for part in name.split("."))):
        raise PublicDNSError()
    return name


def public_address(value):
    if not isinstance(value, str) or "%" in value:
        raise PublicDNSError()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise PublicDNSError() from None
    check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
    if not check.is_global or check.is_multicast or check.is_reserved or check.is_unspecified:
        raise PublicDNSError()
    return str(ip)


def _members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicDNSError()
        result[key] = value
    return result


def parse_answer(raw, hostname, question_type):
    """Require one exact, terminal A/AAAA answer chain; never trust a CNAME alone."""
    if (not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESPONSE_BYTES
            or type(question_type) is not int or question_type not in {1, 28}):
        raise PublicDNSError()
    hostname = canonical_hostname(hostname)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_members,
                           parse_constant=lambda _: (_ for _ in ()).throw(PublicDNSError()))
        if (not isinstance(value, dict) or type(value.get("Status")) is not int or value["Status"] != 0
                or value.get("TC") is not False):
            raise PublicDNSError()
        for flag in ("RD", "RA", "AD", "CD"):
            if flag in value and type(value[flag]) is not bool:
                raise PublicDNSError()
        question = value["Question"]
        if (not isinstance(question, dict) or canonical_hostname(question["name"]) != hostname
                or type(question["type"]) is not int or question["type"] != question_type):
            raise PublicDNSError()
        records = value.get("Answer", [])
        if not isinstance(records, list) or len(records) > MAX_ANSWERS:
            raise PublicDNSError()
        aliases, addresses = {}, []
        for record in records:
            if (not isinstance(record, dict) or type(record.get("type")) is not int
                    or type(record.get("TTL")) is not int or not 0 <= record["TTL"] <= 2**31-1
                    or not isinstance(record.get("data"), str)):
                raise PublicDNSError()
            name = canonical_hostname(record["name"])
            if record["type"] == 5:
                if name in aliases:
                    raise PublicDNSError()
                aliases[name] = canonical_hostname(record["data"])
            elif record["type"] in {1, 28}:
                address = public_address(record["data"])
                if record["type"] != question_type or ipaddress.ip_address(address).version != (4 if question_type == 1 else 6):
                    raise PublicDNSError()
                addresses.append((name, address))
            else:
                raise PublicDNSError()
        terminal, visited = hostname, set()
        while terminal in aliases:
            if terminal in visited or len(visited) >= 8:
                raise PublicDNSError()
            visited.add(terminal)
            terminal = aliases[terminal]
        if set(aliases) != visited or any(name != terminal for name, _ in addresses) or aliases and not addresses:
            raise PublicDNSError()
        return list(dict.fromkeys(address for _, address in addresses))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise PublicDNSError() from None


class _Deadline:
    """One owned socket at a time, interrupted even during a trickling response."""
    def __init__(self, seconds):
        self.end = time.monotonic()+seconds
        self.lock = threading.Lock()
        self.socket = None
        self.expired = False
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True

    @staticmethod
    def close_socket(sock):
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()

    def expire(self):
        with self.lock:
            self.expired = True
            self.close_socket(self.socket)
            self.socket = None

    def remaining(self):
        left = self.end-time.monotonic()
        if self.expired or left <= 0:
            raise PublicDNSError()
        return left

    def adopt(self, sock):
        with self.lock:
            if self.expired or time.monotonic() >= self.end:
                self.close_socket(sock)
                raise PublicDNSError()
            self.socket = sock

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *_):
        self.timer.cancel()
        self.timer.join()
        self.expire()


def _query(hostname, question_type, deadline, byte_limit):
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(RESOLVER_HOST, RESOLVER_PORT, timeout=deadline.remaining(), context=context)

    def connect():
        # Literal IPv4 socket: no OS DNS, environment proxy, or mutable URL.
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        deadline.adopt(raw)
        raw.settimeout(deadline.remaining())
        raw.connect((RESOLVER_IP, RESOLVER_PORT))
        secure = context.wrap_socket(raw, server_hostname=RESOLVER_HOST, do_handshake_on_connect=False)
        deadline.adopt(secure)
        secure.settimeout(deadline.remaining())
        secure.do_handshake()
        connection.sock = secure

    connection.connect = connect
    try:
        connection.request("GET", "/resolve?"+urlencode({"name": hostname, "type": question_type}),
            headers={"Host": RESOLVER_HOST, "Accept": "application/dns-json",
                     "Accept-Encoding": "identity", "Connection": "close", "User-Agent": "Jingban-Agent/0.1"})
        response = connection.getresponse()
        if (response.status != 200
                or (response.getheader("Content-Type") or "").split(";", 1)[0].lower() not in {"application/json", "application/dns-json"}
                or (response.getheader("Content-Encoding") or "identity").lower() != "identity"):
            raise PublicDNSError()
        length = response.getheader("Content-Length")
        transfer = response.getheader("Transfer-Encoding")
        if transfer is not None and (transfer.lower() != "chunked" or length is not None):
            raise PublicDNSError()
        if length is not None and (not length.isdigit() or int(length) > byte_limit):
            raise PublicDNSError()
        body = response.read(byte_limit+1)
        deadline.remaining()
        if len(body) > byte_limit or length is not None and len(body) != int(length):
            raise PublicDNSError()
        return body
    finally:
        connection.close()


def resolve_fake_ip(hostname):
    """Called only after the caller has established an all-Fake-IP OS answer."""
    hostname = canonical_hostname(hostname)
    try:
        with _Deadline(RESOLVE_TIMEOUT) as deadline:
            results, size = [], 0
            for qtype in (1, 28):
                deadline.remaining()
                raw = _query(hostname, qtype, deadline, MAX_RESPONSE_BYTES-size)
                size += len(raw)
                if size > MAX_RESPONSE_BYTES:
                    raise PublicDNSError()
                results.extend(parse_answer(raw, hostname, qtype))
            if not results:
                raise PublicDNSError()
            deadline.remaining()
            return list(dict.fromkeys(results))  # A before AAAA, no system re-resolution.
    except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
        raise PublicDNSError() from None

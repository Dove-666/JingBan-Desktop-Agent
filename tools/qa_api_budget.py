"""Explicit QA-only model relay with a durable, shared, non-refundable quota.

Importing this module performs no IO. It never loads settings or credentials.
The caller supplies one captured upstream base URL/key and an owned ledger.
Only authenticated loopback POST chat/completions is relayed, without redirects
or transport retries. Every upstream attempt is reserved before network IO.
Bodies, API keys and provider text are never persisted or logged here.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, suppress
import hmac
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit, urlunsplit


class BudgetError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_SESSION = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_OUTCOMES = frozenset({"reserved", "completed", "upstream_error", "redirect_refused", "cancelled"})
_UNCONFIRMED = set()
_OWNERS_LOCK = threading.Lock()


def _plain(path):
    """Reject redirected ledger files; the caller still selects the owned path."""
    try:
        value = path.lstat()
        return (path.is_file() and not path.is_symlink()
                and not getattr(value, "st_file_attributes", 0) & 0x400
                and getattr(value, "st_nlink", 1) == 1)
    except OSError:
        return False


class BudgetLedger:
    """SQLite counter: parallel reservations commit once and never refund.

    First creation requires create=True. Later opens default to mode=rw so a
    missing/deleted file cannot silently reset this session's quota. Reopening
    with a different session or quota is refused, including a larger quota.
    """
    def __init__(self, path, *, session_id, limit=100, create=False):
        if (type(session_id) is not str or not _SESSION.fullmatch(session_id)
                or type(limit) is not int or not 1 <= limit <= 100):
            raise BudgetError("BUDGET_CONFIGURATION_INVALID")
        self.path = Path(path).absolute()
        self.session_id, self.limit = session_id, limit
        self._identity = None
        created = False
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("xb"):
                    pass
                created = True
            except FileExistsError:
                pass
        if not _plain(self.path):
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE")
        info = self.path.stat()
        self._identity = (info.st_dev, info.st_ino)
        try:
            with closing(self._connect()) as connection:
                if created:
                    connection.execute("PRAGMA journal_mode=DELETE")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE quota(version INTEGER NOT NULL, session_id TEXT NOT NULL, maximum INTEGER NOT NULL, used INTEGER NOT NULL)")
                    connection.execute("CREATE TABLE attempts(id INTEGER PRIMARY KEY, started_ns INTEGER NOT NULL, outcome TEXT NOT NULL, http_status INTEGER)")
                    connection.execute("INSERT INTO quota VALUES(1,?,?,0)", (session_id, limit))
                    connection.commit()
                self._checked(connection)
        except BudgetError:
            raise
        except (OSError, sqlite3.Error):
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None

    def _connect(self):
        if not _plain(self.path):
            raise BudgetError("BUDGET_LEDGER_CHANGED")
        info = self.path.stat()
        if (info.st_dev, info.st_ino) != self._identity:
            raise BudgetError("BUDGET_LEDGER_CHANGED")
        connection = sqlite3.connect(self.path.as_uri()+"?mode=rw", uri=True, timeout=3)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _checked(self, connection):
        rows = connection.execute("SELECT version,session_id,maximum,used FROM quota").fetchmany(2)
        if (len(rows) != 1 or rows[0][:3] != (1, self.session_id, self.limit)
                or type(rows[0][3]) is not int or not 0 <= rows[0][3] <= self.limit):
            raise BudgetError("BUDGET_LEDGER_IDENTITY_INVALID")
        return rows[0][3]

    def reserve(self):
        """Return an opaque attempt number, or refuse before upstream IO."""
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                used = self._checked(connection)
                if used >= self.limit:
                    raise BudgetError("BUDGET_EXHAUSTED")
                attempt = used+1
                connection.execute("UPDATE quota SET used=?", (attempt,))
                connection.execute("INSERT INTO attempts VALUES(?,?,?,NULL)", (attempt, time.time_ns(), "reserved"))
                connection.commit()
                return attempt
            finally:
                connection.close()
        except BudgetError:
            raise
        except (OSError, sqlite3.Error):
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None

    def finish(self, attempt, outcome, status=None):
        if (type(attempt) is not int or outcome not in _OUTCOMES
                or status is not None and (type(status) is not int or not 100 <= status <= 599)):
            raise BudgetError("BUDGET_OUTCOME_INVALID")
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._checked(connection)
                connection.execute("UPDATE attempts SET outcome=?,http_status=? WHERE id=? AND outcome='reserved'", (outcome, status, attempt))
                connection.commit()
            finally:
                connection.close()
        except BudgetError:
            raise
        except (OSError, sqlite3.Error):
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None

    def snapshot(self):
        try:
            connection = self._connect()
            try:
                used = self._checked(connection)
                outcomes = dict(connection.execute("SELECT outcome,COUNT(*) FROM attempts GROUP BY outcome"))
                if not set(outcomes) <= _OUTCOMES:
                    raise BudgetError("BUDGET_LEDGER_IDENTITY_INVALID")
                return {"session_id": self.session_id, "limit": self.limit, "used": used,
                        "remaining": self.limit-used, "outcomes": outcomes}
            finally:
                connection.close()
        except BudgetError:
            raise
        except (OSError, sqlite3.Error):
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None

    @property
    def count(self):
        return self.snapshot()["used"]


def _upstream_target(base):
    try:
        parsed = urlsplit(base)
        if (type(base) is not str or len(base) > 4096 or any(ord(c) < 33 for c in base)
                or parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0
                or "\\" in parsed.path):
            raise ValueError()
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path+"/chat/completions", "", ""))
    except (TypeError, ValueError):
        raise BudgetError("BUDGET_UPSTREAM_INVALID") from None


def _strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_):
        raise ValueError()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    if type(value) is not dict:
        raise ValueError()


class BudgetProxy:
    """Synchronous context manager around one owned asynchronous relay thread.

    Use base_url/token for every QA client and child; never pass the real key
    or upstream URL to those clients. A separately constructed proxy can reuse
    the same ledger without renewing its budget. No runtime config is saved.
    """
    def __init__(self, ledger, upstream_base_url, upstream_api_key, *, timeout_seconds=60, trust_env=True):
        if (type(ledger) is not BudgetLedger or type(upstream_api_key) is not str or not upstream_api_key
                or any(c in upstream_api_key for c in "\r\n")
                or type(timeout_seconds) not in {int, float} or not 0.1 <= timeout_seconds <= 600):
            raise BudgetError("BUDGET_CONFIGURATION_INVALID")
        self.ledger = ledger
        self._target = _upstream_target(upstream_base_url)
        self._key = upstream_api_key
        self._timeout = timeout_seconds
        self._trust_env = trust_env
        self.token = secrets.token_urlsafe(32)
        self.base_url = None
        self._loop = self._stop = self._engine = None
        self._thread = None
        self._ready = threading.Event()
        self._failure = None
        self._clean = False
        self._entered = False
        self._close_requested = threading.Event()

    def __enter__(self):
        with _OWNERS_LOCK:
            if _UNCONFIRMED:
                raise BudgetError("BUDGET_PROXY_PRIOR_CLEANUP_UNCONFIRMED")
        if self._entered:
            raise BudgetError("BUDGET_PROXY_ALREADY_STARTED")
        self.ledger.snapshot()
        self._entered = True
        # The QA relay must never make transport debug logging a key/body sink.
        names = {"httpx", "httpcore"} | {name for name in logging.Logger.manager.loggerDict
            if name.startswith(("httpx.", "httpcore."))}
        for name in names:
            logger = logging.getLogger(name)
            logger.setLevel(logging.CRITICAL+1)
            logger.disabled = True
        self._thread = threading.Thread(target=self._run, name="aura-qa-budget-relay", daemon=False)
        self._thread.start()
        if not self._ready.wait(10) or self._failure:
            with suppress(BudgetError):
                self.close()
            raise BudgetError("BUDGET_PROXY_START_FAILED")
        return self

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException:
            self._failure = "BUDGET_PROXY_FAILED"
        finally:
            self._ready.set()

    async def _serve(self):
        import httpx
        # Reuse the production proxy's reviewed IOCP accept/socket ownership,
        # without its public-network policy or its forwarding implementation.
        from agent_backend.nanobot.browser_network import PinnedPublicProxy
        owner = self
        class Relay(PinnedPublicProxy):
            async def _accept(self, reader, writer):
                return await owner._handle(self, reader, writer)
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        engine = self._engine = Relay(None)
        client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False, trust_env=self._trust_env)
        try:
            port = await engine.start()
            self.base_url = f"http://127.0.0.1:{port}/v1"
            self._client = client
            self._ready.set()
            if self._close_requested.is_set():
                self._stop.set()
            await self._stop.wait()
        finally:
            try:
                await engine.close()
            finally:
                await client.aclose()
            self._clean = bool(engine._close_complete and client.is_closed)

    async def _error(self, writer, status, code):
        raw = json.dumps({"error": {"message": code, "type": "aura_qa_budget", "code": code}}, separators=(",", ":")).encode("ascii")
        writer.write(f"HTTP/1.1 {status} QA Relay\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode("ascii")+raw)
        await writer.drain()

    async def _handle(self, engine, reader, writer):
        attempt = None
        relay = disconnect = None
        try:
            async with engine.slots:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
                if len(header) > 32768:
                    raise ValueError()
                lines = header[:-4].decode("iso-8859-1").split("\r\n")
                first = lines[0].split(" ")
                if len(first) != 3 or first[2] != "HTTP/1.1":
                    raise ValueError()
                method, path, _ = first
                if method != "POST" or path not in {"/v1/chat/completions", "/chat/completions"}:
                    await self._error(writer, 405, "BUDGET_ENDPOINT_REFUSED")
                    return
                fields = {}
                for line in lines[1:]:
                    key, separator, value = line.partition(":")
                    if (not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                            or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)):
                        raise ValueError()
                    key = key.lower()
                    if key in fields:
                        raise ValueError()
                    fields[key] = value.strip()
                if not hmac.compare_digest(fields.get("authorization", "").encode("utf-8"), ("Bearer "+self.token).encode("ascii")):
                    await self._error(writer, 401, "BUDGET_AUTH_REFUSED")
                    return
                length = fields.get("content-length", "")
                if ("transfer-encoding" in fields or "expect" in fields or not length.isdigit()
                        or not 1 <= int(length) <= 16*1024*1024
                        or fields.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                    raise ValueError()
                body = await asyncio.wait_for(reader.readexactly(int(length)), timeout=self._timeout)
                _strict_json(body)
                try:
                    attempt = await asyncio.to_thread(self.ledger.reserve)
                except BudgetError as error:
                    await self._error(writer, 429 if error.code == "BUDGET_EXHAUSTED" else 503, error.code)
                    return
                relay = asyncio.create_task(self._forward(writer, body, attempt))
                disconnect = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait({relay, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done and relay not in done:
                    relay.cancel()
                await asyncio.gather(relay, return_exceptions=False)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError, RecursionError, UnicodeError):
            with suppress(OSError, ConnectionError):
                await self._error(writer, 400, "BUDGET_REQUEST_INVALID")
        except (OSError, ConnectionError):
            pass
        finally:
            pending = [task for task in (relay, disconnect) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            writer.close()
            with suppress(OSError, ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1)

    async def _forward(self, writer, body, attempt):
        import httpx
        response = None
        headers_sent = False
        outcome, status = "upstream_error", None
        try:
            request = self._client.build_request("POST", self._target, content=body, headers={
                "Authorization": "Bearer "+self._key, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", "Accept-Encoding": "identity"})
            # No redirect following, retry adapter, caller-selected target or
            # second network attempt exists after this durable reservation.
            response = await self._client.send(request, stream=True, follow_redirects=False)
            status = response.status_code
            if not 200 <= status <= 599:
                status = None
                raise ValueError()
            if 300 <= status < 400:
                outcome = "redirect_refused"
                await self._error(writer, 502, "BUDGET_UPSTREAM_REDIRECT_REFUSED")
                return
            if not 200 <= status < 300:
                # QA clients know only the relay token. A provider error can
                # echo the *real* upstream credential, which their usual key
                # scrubbing cannot recognize. Never expose this raw body or
                # its headers; status alone is sufficient for normal retry/
                # authentication handling. The reservation is not refunded.
                await self._error(writer, status, f"BUDGET_UPSTREAM_HTTP_{status}")
                return
            content_type = response.headers.get("content-type", "application/json")
            encoding = response.headers.get("content-encoding")
            if any(c in content_type for c in "\r\n") or encoding and any(c in encoding for c in "\r\n"):
                raise ValueError()
            headers = (f"HTTP/1.1 {status} QA Upstream\r\nContent-Type: {content_type}\r\n"
                       "Transfer-Encoding: chunked\r\nConnection: close\r\n")
            if encoding:
                headers += "Content-Encoding: "+encoding+"\r\n"
            writer.write((headers+"\r\n").encode("iso-8859-1"))
            await writer.drain()
            headers_sent = True
            async for chunk in response.aiter_raw():
                if chunk:
                    writer.write(f"{len(chunk):x}\r\n".encode("ascii")+chunk+b"\r\n")
                    await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (httpx.HTTPError, OSError, ValueError, UnicodeError):
            if not headers_sent:
                with suppress(OSError, ConnectionError):
                    await self._error(writer, 502, "BUDGET_UPSTREAM_FAILED")
            # After headers, close an incomplete chunked response. Do not turn
            # a broken upstream into a normal end of an SSE/JSON response.
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                # A downstream disconnect and server shutdown can both cancel
                # this exact relay. Drain its one owned metadata write rather
                # than leaving the finalizer as an unowned executor future.
                finalizer = asyncio.create_task(asyncio.to_thread(self.ledger.finish, attempt, outcome, status))
                while True:
                    try:
                        await asyncio.shield(finalizer)
                        break
                    except asyncio.CancelledError:
                        if finalizer.done():
                            finalizer.result()
                            break

    def snapshot(self):
        return {**self.ledger.snapshot(), "running": bool(self._thread and self._thread.is_alive()),
                "closed": self._clean, "cleanup_unconfirmed": self in _UNCONFIRMED}

    def close(self):
        if not self._entered:
            return
        self._close_requested.set()
        if self._loop is not None and self._stop is not None and self._thread.is_alive():
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=12)
        if self._thread.is_alive() or not self._clean or self._failure:
            with _OWNERS_LOCK:
                _UNCONFIRMED.add(self)
            raise BudgetError("BUDGET_PROXY_CLEANUP_UNCONFIRMED")
        self._key = ""

    def __exit__(self, kind, value, traceback):
        self.close()


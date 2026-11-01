"""Optional iroh QUIC transport running outside the Qt/UI thread.

Each request occupies one bidirectional QUIC stream. Only the encrypted
connection's remote_id is passed to the application dispatcher. No plaintext
fallback is provided, and exception details / message contents are never logged.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import math
import os
import secrets
import sys
import threading
import time
from typing import Callable

from .addresses import physical_interface_ips, usable_direct_addresses
from .protocol import (ALPN, MAX_CONCURRENT_REQUESTS, MAX_FRAME_BYTES, REQUEST_TIMEOUT,
                       UNPAIRED_MAX_FRAME_BYTES,
                       ProtocolError, error_response, read_frame,
                       validate_request, write_frame)


class TransportError(RuntimeError):
    """A static, user-safe transport failure."""

    def __init__(self, message, code='transport_failed'):
        super().__init__(message)
        self.code = code


DEFAULT_LISTEN_PORT = 43123


_TRACE_STAGES = frozenset(("stream_open", "header_wait", "header_read", "payload_wait", "payload_read",
    "json_valid", "fin_wait", "fin_read", "worker_queued", "dispatch", "dispatch_return",
    "response_wait", "response_written", "protocol_error", "timeout", "stream_error", "stream_done"))
_TRACE_METHODS = frozenset(("pair", "status", "pet.command", "chat.send", "chat.config", "chat.import", "sync.snapshot", "sync.pull",
                           "operation.get", "image.upload", "image.list", "image.get", "image.delete"))


def _load_iroh():
    try:
        return importlib.import_module("iroh")
    except (ImportError, OSError):
        raise TransportError(
            "Encrypted remote support is unavailable. Install requirements-remote.txt.",
            'dependency_unavailable',
        ) from None


def _secret_bytes(secret_hex: str | None) -> bytes:
    if secret_hex is None:
        return secrets.token_bytes(32)
    try:
        if not isinstance(secret_hex, str) or len(secret_hex) != 64:
            raise ValueError
        value = bytes.fromhex(secret_hex)
        if len(value) != 32:
            raise ValueError
        return value
    except ValueError:
        raise TransportError("Invalid encrypted device identity.", 'invalid_identity') from None


def connection_type(connection) -> str:
    """Report only paths actually selected by the SDK, never infer from a ticket."""
    try:
        selected = [path for path in connection.paths() if path.is_selected]
        if selected and all(path.is_relay for path in selected):
            return "relay"
        if selected and all(path.is_ip and not path.is_relay for path in selected):
            return "direct"
    except Exception:
        pass
    return "unknown"


async def _bind_endpoint(iroh, secret: bytes, listen_port: int = 0):
    builder = iroh.EndpointBuilder()
    builder.apply_n0()
    builder.secret_key(secret)
    builder.alpns([ALPN])
    if listen_port:
        builder.bind_addr(f'0.0.0.0:{listen_port}')
    return await asyncio.wait_for(builder.bind(), REQUEST_TIMEOUT)


class IrohTransport:
    """A bounded request server. start() and stop() never wait on network IO.

    on_status may run on the caller or transport thread. on_request runs in a
    bounded worker pool: dispatch Qt work via queued signals, not direct calls.
    stop() requests shutdown; the final callback is emitted after sockets close.
    """

    def __init__(self, secret_hex: str,
                 on_request: Callable[[str, dict], dict],
                 on_status: Callable[[dict], None], *,
                 is_authorized: Callable[[str], bool] | None = None,
                 listen_port: int = DEFAULT_LISTEN_PORT):
        if type(listen_port) is not int or not 0 <= listen_port <= 65535:
            raise TransportError('Invalid encrypted listen port.', 'bind_failed')
        self._listen_port = listen_port
        self._secret_hex = secret_hex
        self._on_request = on_request
        self._on_status = on_status
        # This optional, data-only hook limits allocation. It never grants
        # authorization: the application dispatcher must still check every RPC.
        self._is_authorized = is_authorized
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._endpoint = None
        self._iroh = None
        self._executor: ThreadPoolExecutor | None = None
        self._connections: set[asyncio.Task] = set()
        self._requests: set[asyncio.Task] = set()
        self._workers: set = set()
        self._endpoint_id = ""
        self._ticket = ""
        self._path = "unknown"
        self._relay_ready = False
        self._local_available = False
        self._trace_enabled = os.environ.get("AURA_REMOTE_TRACE") == "1"
        self._trace_counter = 0

    def _trace(self, number, stage, method=None):
        """Opt-in static stages only; never log request ids, peers or payloads."""
        if not self._trace_enabled or stage not in _TRACE_STAGES:
            return
        suffix = "" if method is None else " method=" + (method if method in _TRACE_METHODS else "other")
        try:
            print(f"AURA_REMOTE_TRACE n={int(number)} stage={stage}{suffix}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested.clear()
            self._endpoint_id = self._ticket = ""
            self._path = "unknown"
            self._relay_ready = False
            self._local_available = False
            self._thread = threading.Thread(target=self._run, name="aura-remote-iroh",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        with self._lock:
            loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def wait_stopped(self, timeout: float = 10) -> bool:
        """Join from tests/background code only; never call on the Qt UI thread."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def _publish(self, state: str, error: str = "", error_code: str = "") -> None:
        status = {"state": state, "endpoint_id": self._endpoint_id,
                  "ticket": self._ticket, "error": error,
                  "connection_type": self._path, 'error_code': error_code,
                  'local_available': self._local_available, 'relay_available': self._relay_ready}
        try:
            self._on_status(status)
        except Exception:
            # UI teardown or a callback failure must not expose private state.
            pass

    def _run(self) -> None:
        self._publish("starting")
        try:
            asyncio.run(self._serve())
        except TransportError as exc:
            self._publish("error", str(exc), exc.code)
        except Exception:
            self._publish("error", "Encrypted remote connection could not start.", 'transport_failed')
        else:
            self._publish("stopped")
        finally:
            with self._lock:
                self._loop = None
                self._stop_event = None

    async def _serve(self) -> None:
        self._iroh = _load_iroh()
        if not isinstance(self._secret_hex, str):
                raise TransportError("Invalid encrypted device identity.", 'invalid_identity')
        secret = _secret_bytes(self._secret_hex)
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS,
                                            thread_name_prefix="aura-remote-request")
        tasks: list[asyncio.Task] = []
        try:
            stopping = asyncio.create_task(self._stop_event.wait())
            binding = asyncio.create_task(_bind_endpoint(self._iroh, secret, self._listen_port))
            tasks.extend([binding, stopping])
            done, _ = await asyncio.wait([binding, stopping],
                                         return_when=asyncio.FIRST_COMPLETED)
            if binding in done:
                try:
                    self._endpoint = await binding
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise TransportError('The local encrypted socket could not bind.', 'bind_failed') from None
            if stopping in done:
                return
            self._endpoint_id = str(self._endpoint.id())
            online = asyncio.create_task(self._wait_for_relay())
            tasks.append(online)
            # online() waits for a home relay, not for a usable LAN socket.
            # Holding local startup behind it breaks same-Wi-Fi/offline use.
            if not await self._wait_for_address():
                return
            self._publish("listening")
            accepting = asyncio.create_task(self._accept_loop())
            addresses = asyncio.create_task(self._address_loop())
            tasks.extend([accepting, addresses])
            await asyncio.wait([stopping, accepting, addresses],
                               return_when=asyncio.FIRST_COMPLETED)
            for task in (accepting, addresses):
                if task.done():
                    await task
        finally:
            for task in [*tasks, *self._connections, *self._requests]:
                task.cancel()
            await asyncio.gather(*tasks, *self._connections, *self._requests,
                                 return_exceptions=True)
            if self._endpoint is not None:
                try:
                    await asyncio.wait_for(self._endpoint.close(), 5)
                except Exception:
                    pass
            self._endpoint = None
            self._connections.clear()
            self._requests.clear()
            if self._executor is not None:
                # The Qt controller cancels its queued UI futures before stop.
                # Let those workers exit before publishing a stopped status.
                # Python cannot forcibly terminate an arbitrary stuck callback.
                deadline = time.monotonic() + 6
                while any(not worker.done() for worker in self._workers) and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                settled = all(worker.done() for worker in self._workers)
                self._executor.shutdown(wait=settled, cancel_futures=True)
                self._executor = None
                self._workers.difference_update([worker for worker in self._workers if worker.done()])
                if not settled:
                    raise TransportError("A remote task is still finishing after shutdown.", 'shutdown_pending')

    async def _wait_for_relay(self) -> None:
        try:
            await self._endpoint.online()
            self._relay_ready = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Local authenticated RPC remains available when WAN relays fail.
            self._relay_ready = False

    async def _wait_for_address(self) -> bool:
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while not self._stop_event.is_set():
            self._refresh_ticket()
            if self._ticket:
                return True
            if time.monotonic() >= deadline:
                raise TransportError('No usable direct address or relay is available.', 'no_usable_address')
            try:
                await asyncio.wait_for(self._stop_event.wait(), .2)
            except asyncio.TimeoutError:
                pass
        return False

    def _refresh_ticket(self) -> bool:
        addr = self._endpoint.addr()
        addresses = usable_direct_addresses(addr.direct_addresses(), physical_interface_ips())
        self._local_available = bool(addresses)
        relay = addr.relay_url() if self._relay_ready else None
        safe_addr = self._iroh.EndpointAddr(addr.id(), relay, addresses)
        ticket = str(self._iroh.EndpointTicket.from_addr(safe_addr)) if addresses or relay else ''
        changed = ticket != self._ticket
        self._ticket = ticket
        return changed

    async def _address_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._refresh_ticket():
                if not self._ticket:
                    raise TransportError('The local network is no longer available.', 'no_usable_address')
                self._publish("listening")

    async def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            incoming = await self._endpoint.accept_next()
            if incoming is None:
                return
            if len(self._connections) >= MAX_CONCURRENT_REQUESTS:
                await incoming.refuse()
                continue
            task = asyncio.create_task(self._handle_connection(incoming))
            self._connections.add(task)
            task.add_done_callback(self._connections.discard)

    async def _handle_connection(self, incoming) -> None:
        connection = None
        try:
            accepting = await asyncio.wait_for(incoming.accept(), REQUEST_TIMEOUT)
            if await asyncio.wait_for(accepting.alpn(), REQUEST_TIMEOUT) != ALPN:
                return
            connection = await asyncio.wait_for(accepting.connect(), REQUEST_TIMEOUT)
            connection.set_max_concurrent_bi_streams(MAX_CONCURRENT_REQUESTS)
            connection.set_max_concurrent_uni_streams(0)
            # Reduce ongoing native buffering. Already advertised QUIC credit
            # cannot be retracted, so this is not a total process memory cap.
            # read_exact consumes incrementally and permits larger app frames.
            connection.set_receive_window(256 * 1024)
            peer_id = str(connection.remote_id())
            self._path = connection_type(connection)
            self._publish("listening")
            while not self._stop_event.is_set():
                idle_timeout = 120 if self._authorized(peer_id) else 15
                stream = await asyncio.wait_for(connection.accept_bi(), idle_timeout)
                if len(self._requests) >= MAX_CONCURRENT_REQUESTS:
                    await self._send_error(stream.send(), None, "busy", "Remote request capacity is busy.")
                    await stream.recv().stop(1)
                    continue
                task = asyncio.create_task(self._handle_stream(peer_id, stream, connection))
                self._requests.add(task)
                task.add_done_callback(self._requests.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Disconnection / malformed peers are local connection failures.
            pass
        finally:
            if connection is not None:
                connection.close(0, b"closed")

    def _authorized(self, peer_id: str) -> bool:
        if self._is_authorized is None:
            return True  # Compatibility: generic test servers have no allowlist.
        try:
            return self._is_authorized(peer_id) is True
        except Exception:
            return False

    async def _handle_stream(self, peer_id: str, stream, connection) -> None:
        self._trace_counter += 1
        trace_number = self._trace_counter
        method = None
        self._trace(trace_number, "stream_open")
        request_id = None
        send, recv = stream.send(), stream.recv()
        replied = False
        try:
            async def dispatch():
                nonlocal request_id, replied, method
                limit = MAX_FRAME_BYTES if self._authorized(peer_id) else UNPAIRED_MAX_FRAME_BYTES
                trace_read = (lambda stage: self._trace(trace_number, stage)) if self._trace_enabled else None
                request = validate_request(await read_frame(recv, max_frame_bytes=limit, on_stage=trace_read))
                request_id = request["id"]
                method = request["method"]
                self._trace(trace_number, "json_valid", method)
                # Exactly one request per stream. The sender must finish its half.
                self._trace(trace_number, "fin_wait", method)
                if await recv.read(1):
                    raise ProtocolError("invalid_frame", "One request is permitted per stream.")
                self._trace(trace_number, "fin_read", method)
                # A timed-out application callback can still be running. Keep
                # its slot occupied until it really finishes; do not queue an
                # unbounded number of callbacks behind stalled workers.
                if self._stop_requested.is_set():
                    raise ProtocolError("disabled", "Remote connection is stopping.")
                self._workers.difference_update([worker for worker in self._workers if worker.done()])
                if len(self._workers) >= MAX_CONCURRENT_REQUESTS:
                    raise ProtocolError("busy", "Remote request capacity is busy.")
                loop = asyncio.get_running_loop()
                def run_request():
                    self._trace(trace_number, "dispatch", method)
                    try:
                        return self._on_request(peer_id, request)
                    finally:
                        self._trace(trace_number, "dispatch_return", method)
                self._trace(trace_number, "worker_queued", method)
                worker = self._executor.submit(run_request)
                self._workers.add(worker)
                def completed(future):
                    try:
                        loop.call_soon_threadsafe(self._workers.discard, future)
                    except RuntimeError:
                        pass
                worker.add_done_callback(completed)
                response = await asyncio.wrap_future(worker)
                if (not isinstance(response, dict) or response.get("id") != request_id
                        or type(response.get("ok")) is not bool):
                    raise ProtocolError("internal_error", "Request could not be completed.")
                self._trace(trace_number, "response_wait", method)
                await write_frame(send, response)
                self._trace(trace_number, "response_written", method)
                replied = True
            await asyncio.wait_for(dispatch(), REQUEST_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except ProtocolError as exc:
            self._trace(trace_number, "protocol_error", method)
            await self._send_error(send, request_id, exc.code, exc.message)
            replied = True
        except asyncio.TimeoutError:
            self._trace(trace_number, "timeout", method)
            await self._send_error(send, request_id, "timeout", "Request timed out.")
            replied = True
        except Exception:
            self._trace(trace_number, "stream_error", method)
            await self._send_error(send, request_id, "internal_error", "Request could not be completed.")
            replied = True
        finally:
            self._trace(trace_number, "stream_done", method)
            try:
                await asyncio.wait_for(recv.stop(0), 1)
                if not replied:
                    await asyncio.wait_for(send.reset(0), 1)
            except Exception:
                pass
            path = connection_type(connection)
            if path != self._path and not self._stop_requested.is_set():
                self._path = path
                self._publish("listening")

    @staticmethod
    async def _send_error(send, request_id, code, message) -> None:
        try:
            await asyncio.wait_for(write_frame(send, error_response(request_id, code, message)), 2)
        except Exception:
            pass


class IrohPeerClient:
    """Reusable encrypted development client; no unencrypted alternative."""

    def __init__(self, secret_hex: str | None = None, timeout: float = REQUEST_TIMEOUT):
        self._secret = _secret_bytes(secret_hex)
        requested_timeout = float(timeout)
        if not math.isfinite(requested_timeout) or requested_timeout <= 0:
            raise ValueError("Timeout must be positive.")
        self.timeout = min(requested_timeout, REQUEST_TIMEOUT)
        self._endpoint = None
        self._connection = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.connection_type = "unknown"
        self.endpoint_id = ""
        self.public_relay_configured = False

    async def connect(self, ticket: str) -> None:
        iroh = _load_iroh()
        try:
            addr = iroh.EndpointTicket.from_string(ticket.strip()).endpoint_addr()
            self.public_relay_configured = bool(addr.relay_url())
        except Exception:
            raise TransportError("Invalid encrypted endpoint ticket.") from None
        try:
            if self._endpoint is not None:
                await self.close()
            self._endpoint = await _bind_endpoint(iroh, self._secret)
            self.endpoint_id = str(self._endpoint.id())
            self._connection = await asyncio.wait_for(
                self._endpoint.connect(addr, ALPN), self.timeout)
            self._connection.set_receive_window(256 * 1024)
            self._connection.set_max_concurrent_uni_streams(0)
            # The ticket is an authenticated destination, not a self-reported ID.
            if str(self._connection.remote_id()) != str(addr.id()):
                raise TransportError("Remote encrypted identity did not match.")
            self.connection_type = connection_type(self._connection)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise TransportError("Encrypted connection failed or timed out.") from None

    async def request(self, value: dict) -> dict:
        validate_request(value)
        if self._connection is None:
            raise TransportError("Encrypted peer is not connected.")
        async with self._semaphore:
            async def exchange():
                stream = await self._connection.open_bi()
                async def receive():
                    response = await read_frame(stream.recv())
                    # A rejected length/header has no request id. Expose its
                    # static protocol error instead of waiting for a blocked
                    # large upload to complete against a smaller peer limit.
                    if response.get("id") is None and response.get("ok") is False:
                        code = response.get("error", {}).get("code")
                        messages = {"frame_too_large": "Remote frame limit exceeded; reduce the page or attachment size.",
                                    "invalid_frame": "Remote peer rejected the frame.",
                                    "invalid_request": "Remote peer rejected the request.",
                                    "busy": "Remote request capacity is busy."}
                        if code in messages:
                            raise ProtocolError(code, messages[code])
                    if response.get("id") != value["id"] or type(response.get("ok")) is not bool:
                        raise TransportError("Invalid encrypted response.")
                    if await stream.recv().read(1):
                        raise TransportError("Invalid encrypted response.")
                    return response
                sending = asyncio.create_task(write_frame(stream.send(), value))
                receiving = asyncio.create_task(receive())
                try:
                    _, response = await asyncio.gather(sending, receiving)
                    self.connection_type = connection_type(self._connection)
                    return response
                finally:
                    sending.cancel()
                    receiving.cancel()
                    await asyncio.gather(sending, receiving, return_exceptions=True)
                    try:
                        await asyncio.wait_for(stream.recv().stop(0), 1)
                        if sending.cancelled():
                            await asyncio.wait_for(stream.send().reset(0), 1)
                    except Exception:
                        pass
            try:
                return await asyncio.wait_for(exchange(), self.timeout)
            except (TransportError, ProtocolError):
                raise
            except Exception:
                raise TransportError("Encrypted request failed or timed out.") from None

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close(0, b"closed")
            self._connection = None
        if self._endpoint is not None:
            try:
                await asyncio.wait_for(self._endpoint.close(), 5)
            except Exception:
                pass
            self._endpoint = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


async def request(ticket: str, request: dict, secret_hex: str | None = None,
                  timeout: float = REQUEST_TIMEOUT) -> dict:
    """One-shot second endpoint, useful for integration tests and development."""
    validate_request(request)
    async with IrohPeerClient(secret_hex, timeout) as client:
        await client.connect(ticket)
        return await client.request(request)

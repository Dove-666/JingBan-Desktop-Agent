"""Loopback HTTPS/SSE fixture for the QA Android emulator; no provider calls.

The server accepts one fixed synthetic credential and three fixed QA prompts.
It stores event timings/counts only, never Authorization or request messages.
Certificates are generated in the owned artifact directory and are never added
to Windows trust. Installing its CA is a separate, emulator-only QA operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
import threading
import time


QA_KEY = "aura-qa-stream-only"


def certificates(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    if all((directory / name).is_file() for name in ("qa-ca.pem", "qa-server.pem", "qa-server.key")):
        return
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AURA QA emulator-5560 temporary root")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True).sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "10.0.2.2")]))
        .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("10.0.2.2")),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    (directory / "qa-ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "qa-server.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (directory / "qa-server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class Fixture:
    def __init__(self, directory):
        self.directory, self.lock, self.requests = directory, threading.Lock(), []
        self.events = []

    def record(self, kind, request_id=None, **fields):
        event = {"event": kind, "request_id": request_id, "utc": datetime.now(timezone.utc).isoformat(),
                 "monotonic": time.monotonic(), **fields}
        with self.lock:
            self.events.append(event)
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")

    def state(self):
        with self.lock:
            return {"request_count": len(self.requests), "requests": [
                {key: value for key, value in item.items() if key != "release"} for item in self.requests], "events": list(self.events)}


def serve(directory, port, stop_event=None):
    fixture = Fixture(directory)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.json(fixture.state() if self.path == "/_qa/state" else {"error": "QA_ENDPOINT_ONLY"},
                      200 if self.path == "/_qa/state" else 404)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 262144:
                    return self.json({"error": "QA_BODY_LIMIT"}, 400)
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, TypeError):
                return self.json({"error": "QA_INVALID_JSON"}, 400)
            if self.path == "/_qa/release":
                with fixture.lock:
                    matches = [row for row in fixture.requests if row["id"] == body.get("request_id")]
                for row in matches:
                    row["release"].set()
                return self.json({"released": len(matches)})
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + QA_KEY:
                return self.json({"error": "QA_AUTH_OR_ENDPOINT"}, 403)
            text = next((row.get("content") for row in reversed(body.get("messages", []))
                         if isinstance(row, dict) and row.get("role") == "user"), None)
            known = isinstance(text, str) and text in {"QA_FIRST", "QA_CANCEL", "QA_AFTER"}
            with fixture.lock:
                row = {"id": len(fixture.requests) + 1, "case": text if known else "unexpected", "release": threading.Event(), "first_sent": False,
                       "end_sent": False, "disconnected": False}
                fixture.requests.append(row)
            identity = row["id"]
            fixture.record("request", identity, case=row["case"], stream=body.get("stream") is True)
            if not known or body.get("stream") is not True:
                fixture.record("rejected", identity)
                return self.json({"error": "QA_PROMPT_AND_STREAM_REQUIRED"}, 400)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            def chunk(value):
                self.wfile.write(f"{len(value):x}\r\n".encode() + value + b"\r\n")
                self.wfile.flush()
            def delta(content=None, finish=None):
                value = {"id": "qa-" + str(identity), "object": "chat.completion.chunk", "created": 0,
                    "model": "qa-stream", "choices": [{"index": 0, "delta": {} if content is None else {"content": content},
                                                        "finish_reason": finish}]}
                chunk(("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode("utf-8"))
            try:
                marker = {"QA_FIRST": "QA_FIRST_VISIBLE", "QA_CANCEL": "QA_CANCEL_PARTIAL", "QA_AFTER": "QA_AFTER_COMPLETE"}[text]
                delta('{"reply":"' + marker)
                row["first_sent"] = True
                fixture.record("first_chunk", identity)
                if text != "QA_AFTER":
                    deadline = time.monotonic() + 90
                    while not row["release"].wait(0.25) and time.monotonic() < deadline:
                        chunk(b": qa-heartbeat\n\n")
                else:
                    time.sleep(0.2)
                delta('，回复完整结束。","emotion":"happy"}')
                delta(finish="stop")
                chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                row["end_sent"] = True
                fixture.record("finished", identity)
            except (OSError, ssl.SSLError):
                row["disconnected"] = True
                fixture.record("disconnected", identity)
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(directory / "qa-server.pem"), str(directory / "qa-server.key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    fixture.record("started", port=port, host="127.0.0.1", tls=True)
    print(json.dumps({"fixture": "ready", "host": "127.0.0.1", "port": port, "tls": True}), flush=True)
    try:
        if stop_event is None:
            server.serve_forever(poll_interval=0.1)
        else:
            server.timeout = .2
            while not stop_event.is_set():
                server.handle_request()
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("artifacts/qa-phone-release-stream"))
    parser.add_argument("--port", type=int, default=18763)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    certificates(args.directory)
    if not args.prepare_only:
        serve(args.directory, args.port)

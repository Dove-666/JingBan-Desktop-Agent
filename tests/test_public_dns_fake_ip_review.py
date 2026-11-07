"""Owned TLS/fault fixtures for the narrowly triggered public DNS fallback."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent_backend.nanobot import public_dns as dns
from agent_backend.nanobot import public_web as web
from agent_backend.nanobot.browser_network import PublicBrowserPolicy

HOST = "docs.python.org"
TLS = Path(__file__).parent/"fixtures/public_dns_owned_tls"


def answer(qtype=1):
    return {"Status": 0, "TC": False, "RD": True, "RA": True, "AD": False, "CD": False,
        "Question": {"name": HOST+".", "type": qtype}, "Answer": [
            {"name": HOST+".", "type": 5, "TTL": 30, "data": "terminal.example.org."},
            {"name": "terminal.example.org.", "type": qtype, "TTL": 30,
             "data": "93.184.216.34" if qtype == 1 else "2606:4700:4700::1111"}]}


class OwnedTLSResolver:
    """Test CA is scoped to this fixture client, never installed in OS trust."""
    def __init__(self, mode="normal"):
        self.mode, self.requests, self.sni = mode, [], []
        self.closed = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass
            def do_GET(self):
                owner.requests.append({"method": self.command, "path": self.path,
                    "host": self.headers.get("Host"), "auth": self.headers.get("Authorization"),
                    "proxy_auth": self.headers.get("Proxy-Authorization"), "cookie": self.headers.get("Cookie")})
                qtype = int(parse_qs(urlsplit(self.path).query)["type"][0])
                raw = json.dumps(answer(qtype)).encode()
                if owner.mode == "oversize": raw = b"x"*(dns.MAX_RESPONSE_BYTES+1)
                self.send_response(302 if owner.mode == "redirect" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "http://127.0.0.1/private")
                self.send_header("Content-Length", str(len(raw)+(10 if owner.mode == "truncated" else 0)))
                self.send_header("Connection", "close"); self.end_headers()
                try:
                    if owner.mode == "trickle":
                        for _ in range(200):
                            self.wfile.write(b" "); self.wfile.flush(); time.sleep(.03)
                    else:
                        self.wfile.write(raw); self.wfile.flush()
                except OSError: pass
                finally:
                    self.close_connection = True
                    owner.closed.set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(TLS/"fixture-cert.pem"), str(TLS/"fixture-public-test-key.pem"))
        context.set_servername_callback(lambda sock, name, ctx: self.sni.append(name))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    def __enter__(self): self.thread.start(); return self
    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        if self.requests and self.mode == "trickle":
            if not self.closed.wait(2): raise AssertionError("Owned TLS handler outlived deadline cleanup")
    def trust(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(TLS/"fixture-cert.pem"))
        return context


class PublicDNSReviewTests(unittest.TestCase):
    def parse(self, value, qtype=1):
        return dns.parse_answer(json.dumps(value).encode(), HOST, qtype)

    def test_valid_terminal_chains_return_public_addresses_in_family_order(self):
        self.assertEqual(["93.184.216.34"], self.parse(answer()))
        self.assertEqual(["2606:4700:4700::1111"], self.parse(answer(28), 28))
        empty = answer(28); empty["Answer"] = []
        self.assertEqual([], self.parse(empty, 28))

    def test_status_question_flags_and_record_types_fail_closed(self):
        values = []
        for field, value in (("Status", True), ("Status", 3), ("TC", True), ("TC", 0), ("RD", 1)):
            row = answer(); row[field] = value; values.append(row)
        for field, value in (("name", "other.example.org"), ("type", True), ("type", 28)):
            row = answer(); row["Question"][field] = value; values.append(row)
        for field, value in (("type", True), ("type", 16), ("TTL", True), ("TTL", -1), ("data", 4)):
            row = answer(); row["Answer"][-1][field] = value; values.append(row)
        for row in values:
            with self.subTest(value=row), self.assertRaises(dns.PublicDNSError): self.parse(row)
        for bad in (None, [], {}, {"name":HOST,"type":None}):
            row=answer();row["Question"]=bad
            with self.subTest(question=bad),self.assertRaises(dns.PublicDNSError):self.parse(row)

    def test_all_answer_addresses_must_be_public_even_when_a_good_address_also_exists(self):
        for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "198.18.1.54", "224.0.0.1", "::1", "::ffff:127.0.0.1"):
            row = answer(); item = deepcopy(row["Answer"][-1]); item["data"] = private; row["Answer"].append(item)
            with self.subTest(address=private), self.assertRaises(dns.PublicDNSError): self.parse(row)

    def test_cname_cycles_unrelated_aliases_conflicting_types_and_unterminated_chains_fail(self):
        rows = []
        row = answer(); row["Answer"].pop(); rows.append(row)
        row = answer(); row["Answer"][0]["data"] = HOST; rows.append(row)
        row = answer(); row["Answer"].append({"name":"unrelated.example.org","type":5,"TTL":1,"data":"terminal.example.org"}); rows.append(row)
        row = answer(); row["Answer"][-1]["name"] = "foreign.example.org"; rows.append(row)
        row = answer(); row["Answer"].append(deepcopy(row["Answer"][0])); rows.append(row)
        row = answer(); row["Answer"][-1]["type"] = 28; rows.append(row)
        for row in rows:
            with self.subTest(value=row), self.assertRaises(dns.PublicDNSError): self.parse(row)

    def test_duplicate_json_oversize_truncation_nan_and_invalid_hostnames_are_refused(self):
        raw = json.dumps(answer()).encode()
        for bad in (b'{"Status":0,'+raw[1:], raw[:-3], b"x"*65537, raw.replace(b'"Status": 0', b'"Status": NaN'), raw.decode().encode("utf-16")):
            with self.subTest(data=bad[:30]), self.assertRaises(dns.PublicDNSError): dns.parse_answer(bad, HOST, 1)
        for host in ("127.0.0.1", "localhost", "evil.local", "a..example.org", "bad_name.org", "https://example.org", "x\x00.org", "a"*64+".org", "\ud800.example.org"):
            with self.subTest(host=host), self.assertRaises(dns.PublicDNSError): dns.canonical_hostname(host)

    def test_only_all_fake_nonliteral_system_results_can_trigger_the_fallback(self):
        def records(values): return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(value,443)) for value in values]
        with patch.object(web, "resolve_fake_ip", return_value=["93.184.216.34"]) as fallback:
            with patch.object(web.socket,"getaddrinfo",return_value=records(["198.18.1.54","198.19.1.1"])):
                self.assertEqual("93.184.216.34", web.public_target("https://docs.python.org/x")[3])
            self.assertEqual(1, fallback.call_count)
            for host, addresses in (("docs.python.org",["198.18.1.54","10.0.0.1"]),("docs.python.org",["198.18.1.54","93.184.216.34"]),("docs.python.org",["127.0.0.1"]),("198.18.1.54",["198.18.1.54"])):
                with self.subTest(host=host,addresses=addresses), patch.object(web.socket,"getaddrinfo",return_value=records(addresses)):
                    with self.assertRaises(web.PublicWebError): web.public_target("https://"+host+"/x")
            self.assertEqual(1, fallback.call_count)
        with patch.object(web.socket,"getaddrinfo",return_value=records(["198.18.1.54"])), patch.object(web,"resolve_fake_ip",side_effect=dns.PublicDNSError()):
            with self.assertRaisesRegex(web.PublicWebError,"PUBLIC_DNS_UNAVAILABLE"): web.public_target("https://docs.python.org/x")

    def run_local(self, fixture, *, trust=True):
        with patch.object(dns,"RESOLVER_IP","127.0.0.1"), patch.object(dns,"RESOLVER_PORT",fixture.server.server_port):
            if trust:
                with patch.object(dns.ssl,"create_default_context",fixture.trust): return dns.resolve_fake_ip(HOST)
            return dns.resolve_fake_ip(HOST)

    def test_real_owned_tls_pins_endpoint_checks_sni_and_sends_no_auth_or_proxy_headers(self):
        with OwnedTLSResolver() as fixture, patch.dict("os.environ", {"HTTPS_PROXY":"http://127.0.0.1:1","ALL_PROXY":"http://127.0.0.1:1"}):
            self.assertEqual(["93.184.216.34","2606:4700:4700::1111"], self.run_local(fixture))
            self.assertEqual(["dns.alidns.com"]*2, fixture.sni)
            self.assertEqual(2,len(fixture.requests))
            for row in fixture.requests:
                self.assertEqual("dns.alidns.com",row["host"])
                self.assertIsNone(row["auth"]); self.assertIsNone(row["proxy_auth"]); self.assertIsNone(row["cookie"])

    def test_real_owned_tls_untrusted_certificate_fails_before_any_http(self):
        with OwnedTLSResolver() as fixture:
            with self.assertRaises(dns.PublicDNSError): self.run_local(fixture,trust=False)
            self.assertEqual([],fixture.requests)

    def test_actual_tls_redirect_truncation_and_oversize_are_never_accepted_or_followed(self):
        for mode in ("redirect","truncated","oversize"):
            with self.subTest(mode=mode), OwnedTLSResolver(mode) as fixture:
                with self.assertRaises(dns.PublicDNSError): self.run_local(fixture)
                self.assertEqual(1,len(fixture.requests))

    def test_total_deadline_interrupts_a_live_trickling_tls_body_and_closes_the_socket(self):
        with OwnedTLSResolver("trickle") as fixture, patch.object(dns,"RESOLVE_TIMEOUT",.2):
            started=time.monotonic()
            with self.assertRaises(dns.PublicDNSError): self.run_local(fixture)
            self.assertLess(time.monotonic()-started,1.5)
            self.assertEqual(1,len(fixture.requests))

    def test_queries_share_one_deadline_and_one_combined_body_budget(self):
        def slow_query(host,qtype,deadline,byte_limit):
            time.sleep(.08)
            return json.dumps(answer(qtype)).encode()
        with patch.object(dns,"RESOLVE_TIMEOUT",.1),patch.object(dns,"_query",slow_query):
            with self.assertRaises(dns.PublicDNSError):dns.resolve_fake_ip(HOST)
        def oversized_pair(host,qtype,deadline,byte_limit):
            raw=json.dumps(answer(qtype)).encode()
            return raw+b" "*(40000-len(raw))
        with patch.object(dns,"_query",oversized_pair):
            with self.assertRaises(dns.PublicDNSError):dns.resolve_fake_ip(HOST)

    def test_browser_policy_reuses_the_same_public_ip_decision_without_an_allowlist_change(self):
        records=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("198.18.1.54",443))]
        with patch.object(web.socket,"getaddrinfo",return_value=records), patch.object(web,"resolve_fake_ip",return_value=["93.184.216.34"]):
            self.assertEqual("93.184.216.34",PublicBrowserPolicy().target("https://docs.python.org/x")[3])


if __name__ == "__main__": unittest.main()

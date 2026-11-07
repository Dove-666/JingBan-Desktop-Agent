"""Public web SSRF/content rules tested without external network access."""
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_web import fetch_public_text, public_target, PublicWebError, PublicWebReader, MAX_BYTES, MAX_CACHED_SOURCES
from context_store import ObservationStore, namespace_for, ContextStoreError


def public_dns(host, port, **_kwargs):
    ip = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


class Response:
    def __init__(self, body=b"Hello", status=200, **headers):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = {"Content-Type": "text/plain", **headers}
    def getheader(self, key):
        return self.headers.get(key)
    def read1(self, limit):
        return self.body.read(limit)


class Connection:
    responses = []
    created = []
    def __init__(self, host, port, **kwargs):
        self.host = host
        self.closed = False
        self.created.append(self)
    def request(self, method, path, **kwargs):
        self.method, self.path, self.headers = method, path, kwargs["headers"]
    def getresponse(self):
        return self.responses.pop(0)
    def close(self):
        self.closed = True


class PublicWebTests(unittest.TestCase):
    def setUp(self):
        Connection.responses = []
        Connection.created = []

    def test_file_localhost_credentials_and_private_addresses_rejected(self):
        for url in ("file:///etc/passwd", "http://localhost/a", "https://x:y@example.org/a", "http://127.0.0.1/a", "http://169.254.169.254/a"):
            address = "169.254.169.254" if "169.254" in url else "127.0.0.1"
            with self.subTest(url=url), patch("public_web.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]):
                with self.assertRaises(PublicWebError):
                    public_target(url)

    def test_mixed_public_private_dns_rejected(self):
        dns = public_dns("example.org", 443) + public_dns("127.0.0.1", 443)
        with patch("public_web.socket.getaddrinfo", return_value=dns):
            with self.assertRaises(PublicWebError):
                public_target("https://example.org")

    def test_html_text_only_and_pinned_ip_connection(self):
        Connection.responses = [Response(b"<h1>Hello</h1><script>execute_command()</script><p>World</p>", **{"Content-Type": "text/html"})]
        with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection), patch("public_web.socket.create_connection") as connect:
            data = json.loads(fetch_public_text("https://example.org/path"))
            self.assertIn("Hello", data["text"])
            self.assertNotIn("execute_command", data["text"])
            self.assertTrue(data["untrusted_external_data"])
            connection = Connection.created[0]
            connection._create_connection(("rebind.invalid", 443), timeout=3)
            self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
            self.assertTrue(connection.closed)
            self.assertNotIn("Authorization", connection.headers)

    def test_redirect_private_target_revalidated_before_second_connection(self):
        Connection.responses = [Response(status=302, Location="http://127.0.0.1/admin")]
        with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection), patch("public_web.http.client.HTTPConnection", Connection):
            with self.assertRaises(PublicWebError):
                fetch_public_text("https://example.org")
            self.assertEqual(len(Connection.created), 1)

    def test_large_response_bounded_and_binary_rejected(self):
        Connection.responses = [Response(b"x" * (MAX_BYTES * 2)), Response(b"binary", **{"Content-Type": "application/octet-stream"})]
        with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection):
            data = json.loads(fetch_public_text("https://example.org/large"))
            self.assertEqual(len(data["text"]), 16000)
            self.assertTrue(data["truncated"])
            with self.assertRaises(PublicWebError):
                fetch_public_text("https://example.org/program.exe")

    def test_invalid_page_parameters_are_rejected_before_any_network_for_both_readers(self):
        reader = PublicWebReader()
        self.addCleanup(reader.close)
        cases = [(-1, 100), (True, 100), ("0", 100), (0, 0), (0, 24001), (0, True)]
        with patch("public_web.socket.getaddrinfo") as dns, patch("public_web.http.client.HTTPSConnection") as connect:
            for offset, limit in cases:
                for read in (reader.read, fetch_public_text):
                    with self.subTest(read=read.__name__, offset=offset, limit=limit), self.assertRaises(PublicWebError) as caught:
                        read("https://example.org/never-fetched", offset=offset, limit=limit)
                    self.assertEqual(caught.exception.code, "INVALID_WEB_PAGE")
            dns.assert_not_called()
            connect.assert_not_called()

    def test_lru_keeps_only_eight_sources_and_evicted_snapshot_pages_preserve_the_entire_tail(self):
        text = "原始开头\n" + "资料abc\n" * 12000 + "原始尾部-END"
        originals = [text + str(index) for index in range(10)]
        urls = [f"https://example.org/source/{index}" for index in range(10)]
        Connection.responses = [Response(value.encode()) for value in originals] + [Response(b"changed later HTTP content")]
        with tempfile.TemporaryDirectory(prefix="aura-web-store-test-") as directory:
            store = ObservationStore(directory, namespace_for(str(uuid4()), "a" * 64))
            reader = PublicWebReader(store)
            self.addCleanup(reader.close)
            with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection):
                first = json.loads(reader.read(urls[0], limit=1000))
                for url in urls[1:8]:
                    reader.read(url, limit=1000)
                reader.read(urls[0], offset=1000, limit=1000)  # LRU access protects source 0.
                reader.read(urls[8], limit=1000)
                self.assertEqual(len(reader.sources), MAX_CACHED_SOURCES)
                self.assertIn(urls[0], reader.sources)
                self.assertNotIn(urls[1], reader.sources)
                reader.read(urls[9], limit=1000)
                self.assertEqual(len(Connection.created), 10)
                offset, parts = 0, []
                while True:
                    page = json.loads(reader.read(urls[1], offset=offset, limit=13777))
                    self.assertEqual(page["source_sha256"], __import__("hashlib").sha256(originals[1].encode()).hexdigest())
                    parts.append(page["text"])
                    if page["next_offset"] is None:
                        break
                    offset = page["next_offset"]
                self.assertEqual("".join(parts), originals[1])
                self.assertEqual(len(Connection.created), 10, "Old snapshots must never make a new HTTP request")
                self.assertTrue(page["text"].endswith("原始尾部-END1"))
                self.assertFalse(page["truncated"])
                self.assertEqual(len(reader.sources), MAX_CACHED_SOURCES)
                self.assertIn("reference", first)
            index_directory = Path(reader._index_directory.name)
            reader.close()
            self.assertFalse(index_directory.exists())

    def test_without_store_evicted_urls_fail_explicitly_and_are_never_refetched(self):
        reader = PublicWebReader()
        self.addCleanup(reader.close)
        urls = [f"https://example.org/no-store/{index}" for index in range(9)]
        Connection.responses = [Response((f"snapshot-{index}-tail").encode()) for index in range(9)]
        with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection):
            for url in urls:
                reader.read(url, limit=8)
            self.assertEqual(len(reader.sources), 8)
            for offset in (0, 8):
                with self.assertRaises(PublicWebError) as caught:
                    reader.read(urls[0], offset=offset)
                self.assertEqual(caught.exception.code, "WEB_SOURCE_EVICTED")
            page = json.loads(reader.read(urls[-1], offset=8))
            self.assertEqual(page["text"], "-8-tail")
            self.assertEqual(len(Connection.created), 9)

    def test_html_links_and_hidden_content_remain_the_same_after_snapshot_eviction(self):
        html = ("<h1>可见标题</h1><script>script_secret</script><style>style_secret</style>"
            "<noscript>noscript_secret</noscript><svg><text>svg_secret</text></svg>"
            "<a href='/guide'><b>文</b>档</a><a href='javascript:bad()'>不可点击</a>"
            "<a href='https://user:pass@example.org/private'>凭据链接</a><p>可见尾部</p>")
        Connection.responses = [Response(html.encode(), **{"Content-Type": "text/html"})] + [Response(str(i).encode()) for i in range(8)]
        with tempfile.TemporaryDirectory(prefix="aura-web-html-test-") as directory:
            reader = PublicWebReader(ObservationStore(directory, namespace_for(str(uuid4()), "b" * 64)))
            self.addCleanup(reader.close)
            with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection):
                first = json.loads(reader.read("https://example.org/start"))
                self.assertFalse(any(word in first["text"] for word in ("script_secret", "style_secret", "noscript_secret", "svg_secret")))
                self.assertEqual(first["links"], [{"url": "https://example.org/guide", "text": "文 档"}])
                for index in range(8):
                    reader.read(f"https://example.org/other/{index}")
                later = json.loads(reader.read("https://example.org/start"))
                self.assertEqual(later, first)
                self.assertEqual(len(Connection.created), 9)

    def test_archive_redaction_does_not_change_snapshot_text_halfway_through_pagination(self):
        secret = "owned-fixture-private-token"
        text = "前缀" + secret + "data\n" * 10000 + "末尾"
        expected = text.replace(secret, "[redacted]")
        Connection.responses = [Response(text.encode())] + [Response(str(i).encode()) for i in range(8)]
        with tempfile.TemporaryDirectory(prefix="aura-web-clean-test-") as directory:
            store = ObservationStore(directory, namespace_for(str(uuid4()), "c" * 64), secrets=(secret,))
            reader = PublicWebReader(store)
            self.addCleanup(reader.close)
            with patch("public_web.socket.getaddrinfo", side_effect=public_dns), patch("public_web.http.client.HTTPSConnection", Connection):
                first = json.loads(reader.read("https://example.org/clean", limit=1000))
                self.assertEqual(first["text"], expected[:1000])
                for index in range(8):
                    reader.read(f"https://example.org/other/{index}")
                second = json.loads(reader.read("https://example.org/clean", offset=1000, limit=24000))
                self.assertEqual(first["total_chars"], len(expected))
                self.assertEqual(second["text"], expected[1000:25000])
                self.assertEqual(len(Connection.created), 9)
                ObservationStore.clear_conversation(directory, store.conversation_id)
                with self.assertRaises(ContextStoreError):
                    reader.read("https://example.org/clean", offset=25000)
                self.assertEqual(len(Connection.created), 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)

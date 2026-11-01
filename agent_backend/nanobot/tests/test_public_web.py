"""Public web SSRF/content rules tested without external network access."""
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_web import fetch_public_text, public_target, PublicWebError, MAX_BYTES


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

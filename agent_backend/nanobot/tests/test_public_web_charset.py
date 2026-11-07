"""Explicit HTTP charset behavior through the actual offline public reader."""
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_web
from test_public_web import Connection, Response, public_dns


class PublicWebCharsetTests(unittest.TestCase):
    def setUp(self):
        Connection.responses = []
        Connection.created = []

    @contextmanager
    def reader(self, body, content_type):
        Connection.responses = [Response(body, **{"Content-Type": content_type})]
        with patch("public_web.socket.getaddrinfo", side_effect=public_dns) as dns, patch("public_web.http.client.HTTPSConnection", Connection):
            reader = public_web.PublicWebReader()
            try:
                yield reader, dns
            finally:
                reader.close()

    def read(self, body, content_type):
        with self.reader(body, content_type) as (reader, _dns):
            result = json.loads(reader.read("https://example.org/owned"))
        self.assertTrue(Connection.created[-1].closed)
        return result

    def test_gb18030_and_gbk_html_preserve_chinese_title_body_and_link(self):
        html = "<title>中文标题</title><p>报名地点和活动说明。</p><a href='/details'>查看详情</a>"
        for encoding in ("gb18030", "gbk"):
            with self.subTest(encoding=encoding):
                result = self.read(html.encode(encoding), f"text/html; charset={encoding}")
                self.assertEqual(result["text"], "中文标题\n报名地点和活动说明。\n查看详情")
                self.assertEqual(result["links"], [{"url": "https://example.org/details", "text": "查看详情"}])

    def test_quoted_case_and_surrounding_ascii_whitespace_are_normalized(self):
        for label in ('" GbK "', '"GB18030"', "Cp936"):
            with self.subTest(label=label):
                result = self.read("中文正文".encode("gbk"), f"Text/Plain; ChArSeT = {label}")
                self.assertEqual(result["text"], "中文正文")

    def test_gb2312_label_uses_gbk_web_compatibility_mapping(self):
        text = "中文镕"
        with self.assertRaises(UnicodeEncodeError):
            text.encode("gb2312")
        result = self.read(text.encode("gbk"), "text/plain; charset=gb2312")
        self.assertEqual(result["text"], text)

    def test_utf8_aliases_and_missing_charset_preserve_original_behavior(self):
        raw = "中文与 emoji 🐳".encode("utf-8")
        for content_type in ("text/plain", "text/plain; charset=utf-8", 'text/plain; charset="UTF8"'):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.read(raw, content_type)["text"], raw.decode("utf-8"))

    def test_duplicate_charsets_same_or_conflicting_use_utf8_fallback(self):
        raw = "中文正文".encode("gbk")
        for parameters in ("charset=gbk; charset=gbk", "charset=gbk; CHARSET=utf-8"):
            with self.subTest(parameters=parameters):
                result = self.read(raw, "text/plain; " + parameters)
                self.assertEqual(result["text"], raw.decode("utf-8", errors="replace"))

    def test_extended_charsets_alone_or_alongside_plain_use_utf8_fallback(self):
        raw = "中文正文".encode("gbk")
        for parameters in ("charset*=utf-8''gbk", "charset=gbk; charset*=utf-8''gbk",
                           "charset*0=gb; charset*1=k", "charset*; charset=gbk"):
            with self.subTest(parameters=parameters):
                result = self.read(raw, "text/plain; " + parameters)
                self.assertEqual(result["text"], raw.decode("utf-8", errors="replace"))

    def test_extended_marker_inside_quoted_value_is_not_a_parameter(self):
        raw = "中文正文".encode("gbk")
        for note in ('"value; charset*0=gb; charset*1=k"', '"value \\"; charset*0=gb"'):
            with self.subTest(note=note):
                result = self.read(raw, f"text/plain; note={note}; charset=gbk")
                self.assertEqual(result["text"], "中文正文")

    def test_unknown_unsafe_empty_and_non_ascii_labels_use_utf8_fallback(self):
        labels = ("utf-7", "unicode_escape", "raw_unicode_escape", "base64", "hex", "zlib", "rot13", "mbcs",
                  "unknown-charset", '""', '"gbk;other=value"', '"gb\u212a"', '"' + "g" * 100 + '"')
        for raw in (b"+Ti1lhw- \\u4e2d \\x41 5Lit5paH e4b8ad", "中文正文".encode("utf-8")):
            for label in labels:
                with self.subTest(label=label, body=raw):
                    result = self.read(raw, "text/plain; charset=" + label)
                    self.assertEqual(result["text"], raw.decode("utf-8", errors="replace"))

    def test_json_keeps_utf8_despite_non_utf8_charset_parameter(self):
        raw = '{"title":"中文标题"}'.encode("utf-8")
        for label in ("gbk", "gb18030", "cp936", "gb2312", "utf-7"):
            with self.subTest(label=label):
                result = self.read(raw, f"application/json; charset={label}")
                self.assertEqual(result["text"], raw.decode("utf-8"))

    def test_accepted_xml_uses_explicit_http_charset_without_xml_sniffing(self):
        text = "<root>中文正文</root>"
        result = self.read(text.encode("gbk"), "application/xml; charset=gbk")
        self.assertEqual(result["text"], text)

    def test_multiple_content_type_headers_use_utf8_fallback(self):
        class HeaderResponse(Response):
            getheader = http.client.HTTPResponse.getheader

        raw = "中文正文".encode("gbk")
        response = HeaderResponse(raw)
        response.headers = http.client.HTTPMessage()
        response.headers["Content-Type"] = "text/plain"
        response.headers["Content-Type"] = "text/plain; charset=gbk"
        with self.reader(raw, "text/plain") as (reader, _dns):
            Connection.responses = [response]
            result = json.loads(reader.read("https://example.org/owned"))
        self.assertEqual(result["text"], raw.decode("utf-8", errors="replace"))

    def test_pagination_cache_source_hash_and_hidden_html_remain_consistent(self):
        raw = "<h1>活动安排</h1><script>隐藏文字</script><a href='/next'>查看报名详情</a>".encode("gb18030")
        with self.reader(raw, "text/html; charset=gb18030") as (reader, dns):
            first = json.loads(reader.read("https://example.org/owned", limit=3))
            pages = [first]
            while pages[-1]["next_offset"] is not None:
                pages.append(json.loads(reader.read("https://example.org/owned", offset=pages[-1]["next_offset"], limit=3)))
            self.assertEqual("".join(page["text"] for page in pages), "活动安排\n查看报名详情")
            self.assertTrue(all(page["source_sha256"] == hashlib.sha256(raw).hexdigest() for page in pages))
            self.assertEqual(json.loads(reader.read("https://example.org/owned", limit=3)), first)
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(len(Connection.created), 1)
            self.assertEqual(len(Connection.responses), 0)
        self.assertTrue(Connection.created[0].closed)

    def test_byte_boundary_truncates_before_decoding_and_preserves_raw_hash(self):
        boundary = public_web.MAX_BYTES
        raw = b"a" * (boundary - 1) + "中".encode("gb18030")
        with self.reader(raw, "text/plain; charset=gb18030") as (reader, _dns):
            result = json.loads(reader.read("https://example.org/owned", offset=boundary - 8, limit=16))
        self.assertEqual(result["text"], "a" * 7 + "\ufffd")
        self.assertEqual(result["total_chars"], boundary)
        self.assertTrue(result["source_truncated"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw[:boundary]).hexdigest())


if __name__ == "__main__":
    unittest.main()

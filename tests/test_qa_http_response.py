"""QA response-buffer candidate: final headers, ownership and bounded failure."""
import asyncio
import unittest
from tools.qa_http_response import relay_final_http_response, GATEWAY_UNKNOWN


class Writer:
    def __init__(self): self.data = bytearray()
    def write(self, raw): self.data.extend(raw)
    async def drain(self): pass


class QaHttpResponseTests(unittest.IsolatedAsyncioTestCase):
    async def relay(self, raw, **kwargs):
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(raw); reader.feed_eof()
        body_calls = []
        async def body(source, target):
            body_calls.append(True)
            target.write(await source.read())
        result = await relay_final_http_response(reader, writer, body, **kwargs)
        return result, bytes(writer.data), body_calls

    async def test_complete_final_headers_and_remaining_body_forward_exactly_once(self):
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nBODY"
        result, output, body = await self.relay(raw)
        self.assertTrue(result); self.assertEqual(raw, output); self.assertEqual([True], body)

    async def test_interim_headers_are_private_until_real_final_headers_exist(self):
        interim = b"HTTP/1.1 103 Early Hints\r\n\r\nHTTP/1.1 100 Continue\r\n\r\n"
        final = b"HTTP/1.1 204 No Content\r\n\r\n"
        result, output, body = await self.relay(interim + final)
        self.assertTrue(result); self.assertEqual(interim + final, output)
        result, output, body = await self.relay(interim)
        self.assertFalse(result); self.assertEqual(GATEWAY_UNKNOWN, output); self.assertEqual([], body)

    async def test_empty_partial_malformed_and_excessive_headers_emit_only_explicit_unknown(self):
        for raw in (b"", b"HTTP/1.1 200 OK\r\nContent-Length: 5", b"HTTP/2 200 OK\r\n\r\n",
                    b"HTTP/1.1 101 Switching Protocols\r\n\r\n", b"HTTP/1.1 200 OK\r\nInvalidHeader\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n",
                    b"HTTP/1.1 103 Hints\r\n\r\n" * 9,
                    b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 32768 + b"\r\n\r\n"):
            with self.subTest(prefix=raw[:25]):
                result, output, body = await self.relay(raw)
                self.assertFalse(result); self.assertEqual(GATEWAY_UNKNOWN, output); self.assertEqual([], body)

    async def test_header_deadline_is_bounded_and_cancellation_does_not_emit_a_fake_response(self):
        reader, writer = asyncio.StreamReader(), Writer()
        async def body(*_): raise AssertionError("No body relay before final headers")
        result = await relay_final_http_response(reader, writer, body, timeout=.02)
        self.assertFalse(result); self.assertEqual(GATEWAY_UNKNOWN, writer.data)
        writer = Writer()
        task = asyncio.create_task(relay_final_http_response(reader, writer, body, timeout=2))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(b"", writer.data)


if __name__ == "__main__": unittest.main()

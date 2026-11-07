"""Independent response framing/cancellation checks for the HTTP relay."""
import asyncio
import unittest

from agent_backend.nanobot.http_response import (
    GATEWAY_UNKNOWN, HttpResponseState, relay_final_http_response,
)


class _Writer:
    def __init__(self): self.data = bytearray()
    def write(self, value): self.data.extend(value)
    async def drain(self): pass


class HttpResponseIndependentReviewTests(unittest.IsolatedAsyncioTestCase):
    async def relay(self, raw):
        reader, writer = asyncio.StreamReader(), _Writer()
        reader.feed_data(raw); reader.feed_eof()
        calls = []
        async def body(source, target):
            calls.append(True); target.write(await source.read())
        value = await relay_final_http_response(reader, writer, body, state=HttpResponseState())
        return value, bytes(writer.data), calls

    async def test_conflicting_transfer_encoding_and_length_are_not_forwarded(self):
        # RFC 9112 section 6.3 requires rejecting or removing Content-Length
        # before an intermediary forwards a transfer-encoded response.
        raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n")
        value, output, calls = await self.relay(raw)
        self.assertFalse(value)
        self.assertEqual(GATEWAY_UNKNOWN, output)
        self.assertEqual([], calls)

    async def test_transfer_encoding_forbidden_on_informational_or_204_response(self):
        for status, following in ((b"103 Early Hints", b"HTTP/1.1 200 OK\r\n\r\n"),
                                  (b"204 No Content", b"")):
            with self.subTest(status=status):
                value, output, calls = await self.relay(
                    b"HTTP/1.1 " + status + b"\r\nTransfer-Encoding: chunked\r\n\r\n" + following)
                self.assertFalse(value)
                self.assertEqual(GATEWAY_UNKNOWN, output)
                self.assertEqual([], calls)

    async def test_valid_chunked_response_keeps_its_original_body_once(self):
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n"
        value, output, calls = await self.relay(raw)
        self.assertTrue(value)
        self.assertEqual(raw, output)
        self.assertEqual([True], calls)

    async def test_content_length_is_rejected_on_informational_and_204_responses(self):
        for status, following in ((b"103 Early Hints", b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
                                  (b"204 No Content", b"")):
            with self.subTest(status=status):
                value, output, calls = await self.relay(
                    b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n" + following)
                self.assertFalse(value)
                self.assertEqual(GATEWAY_UNKNOWN, output)
                self.assertEqual([], calls)

    async def test_cancel_during_final_header_drain_never_adds_another_response(self):
        reader, writer, state = asyncio.StreamReader(), _Writer(), HttpResponseState()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"); reader.feed_eof()
        entered = asyncio.Event()
        async def drain(): entered.set(); await asyncio.Event().wait()
        writer.drain = drain
        async def body(*_): raise AssertionError("Body must not run after cancellation")
        task = asyncio.create_task(relay_final_http_response(reader, writer, body, state=state))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("owned-review-stop")
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(state.final_headers_forwarded)
        self.assertEqual(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", writer.data)


if __name__ == "__main__": unittest.main()

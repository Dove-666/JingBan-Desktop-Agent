"""Optional render failures need host-owned no-publication/settlement proof."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_backend.nanobot import office_render


class OfficeRenderFailureScopeTests(unittest.IsolatedAsyncioTestCase):
    async def fail(self, code, *, settled=True, published=False, owned_cleanup=True):
        async def controlled(*args, _execution=None, **kwargs):
            _execution.update(helper_settled=settled, publication_started=published, owned_cleanup_confirmed=owned_cleanup)
            raise office_render.OfficeRenderError(code)
        with patch.object(office_render, "_render_document", controlled):
            with self.assertRaises(office_render.OfficeRenderError) as error:
                await office_render.render_document(None, "synthetic.docx")
        return error.exception

    async def test_private_aborted_helper_may_report_no_published_output(self):
        error = await self.fail("OFFICE_STARTUP_INTERACTION_REQUIRED")
        self.assertTrue(error.safe_no_publication)
        error = await self.fail("OFFICE_TIMEOUT")
        self.assertTrue(error.safe_no_publication)

    async def test_unknown_helper_or_publication_state_never_gets_optional_flag(self):
        for options in ({"settled": False}, {"published": True}, {"owned_cleanup": False}):
            with self.subTest(options=options):
                error = await self.fail("OFFICE_TIMEOUT", **options)
                self.assertFalse(error.safe_no_publication)

    async def test_ownership_protocol_and_generic_failure_remain_blocking(self):
        for code in ("OFFICE_OWNERSHIP_UNCONFIRMED", "OFFICE_PROTOCOL", "OFFICE_RENDER_FAILED",
                     "OFFICE_ISOLATION_UNAVAILABLE", "OFFICE_CLEANUP_UNCONFIRMED"):
            with self.subTest(code=code):
                self.assertFalse((await self.fail(code)).safe_no_publication)

    async def test_model_error_text_cannot_mint_a_host_proof(self):
        async def forged(*args, **kwargs):
            raise ValueError("OFFICE_STARTUP_INTERACTION_REQUIRED")
        with patch.object(office_render, "_render_document", forged):
            with self.assertRaises(ValueError) as error:
                await office_render.render_document(None, "synthetic.docx")
        self.assertFalse(getattr(error.exception, "safe_no_publication", False))


if __name__ == "__main__":
    unittest.main()

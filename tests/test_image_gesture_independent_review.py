"""Independent admission rejection protects an existing owned image and draft."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_chat_image_data_stage as fixture


class ImageGestureIndependentReviewTests(unittest.TestCase):
    setUpClass = fixture.ChatImageDataStageTests.__dict__["setUpClass"]
    setUp = fixture.ChatImageDataStageTests.setUp
    rpc = fixture.ChatImageDataStageTests.rpc
    configure = fixture.ChatImageDataStageTests.configure
    payload = fixture.ChatImageDataStageTests.payload
    stage = fixture.ChatImageDataStageTests.stage
    finish = fixture.ChatImageDataStageTests.finish
    set_draft = fixture.ChatImageDataStageTests.set_draft

    def tearDown(self):
        owned = self.path.resolve()
        self.assertEqual(owned.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(owned.name.startswith("aura-chat-image-"))
        fixture.ChatImageDataStageTests.tearDown(self)

    def test_rejected_authority_or_header_preserves_existing_stage_pixels_and_draft(self):
        text = "Owned unsent draft\n  preserves whitespace"
        self.set_draft(text)
        valid = self.payload()
        self.assertTrue(self.stage(valid)); self.finish()
        staged = deepcopy(self.panel._current_staged_image())
        pixels = self.chat._image_inbox.get(staged["image_id"])
        messages = self.chat.store.load_messages(self.cid)
        variants = [{**valid, "conversationId": "foreign-cid"},
                    {**valid, "draftEpoch": "stale-epoch"},
                    {**valid, "draftDocument": "stale-document"},
                    {**valid, "path": "C:/OWNED_NOT_A_PATH_TO_READ.png"},
                    {**valid, "url": "https://OWNED_INVALID_EXAMPLE/image.png"},
                    {**valid, "mimeType": "image/svg+xml"},
                    {**valid, "filename": "bad\nname.png"},
                    {**valid, "dataBase64": "data:image/png;base64," + valid["dataBase64"]}]
        with patch.object(self.chat, "import_image_data") as importer, \
                patch("desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName") as picker:
            for payload in variants:
                with self.subTest(changed_fields=[key for key in payload if payload[key] != valid.get(key)]):
                    self.assertFalse(json.loads(self.panel.bridge.dispatch("stageImageData", json.dumps(payload)))["ok"])
                    self.assertEqual(self.panel._current_staged_image(), staged)
                    self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, text)
                    self.assertFalse(self.panel._image_preparing)
            importer.assert_not_called(); picker.assert_not_called()
        self.assertEqual(self.chat._image_inbox.get(staged["image_id"]), pixels)
        self.assertEqual(self.chat.store.load_messages(self.cid), messages)
        self.assertFalse(self.chat.busy)


if __name__ == "__main__": unittest.main()

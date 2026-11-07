"""Character card data and corrupt-container regressions using synthetic files."""

import base64
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from desktop_pet.character_cards import (
    CharacterCard, CharacterCardError, MAX_CARD_FILE_BYTES, MAX_CARD_JSON_BYTES,
    MAX_CARD_TEXT_CHARACTERS, PNG_SIGNATURE, load_character_card, parse_card_bytes,
)


def card_document(name="测试角色", *, version=2):
    return {"spec": f"chara_card_v{version}", "spec_version": f"{version}.0", "data": {
        "name": name, "description": "来自海边的小魔女。", "personality": "温柔，偶尔调皮。",
        "scenario": "一起旅行。", "first_mes": "{{user}}，今天去哪儿？", "mes_example": "<START>\n{{char}}: 你好。",
        "system_prompt": "{{original}}\n认真扮演角色。", "post_history_instructions": "使用轻松的语气。",
        "creator_notes": "这段作者说明供用户阅读。", "alternate_greetings": ["早上好。", "晚上好。"],
        "extensions": {"example": {"enabled": True}},
    }}


def json_bytes(document):
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)


HEADER = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
IMAGE = chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
END = chunk(b"IEND", b"")


def png_bytes(document=None, *, keyword=b"chara", kind=b"tEXt", additional=()):
    metadata = b""
    if document is not None:
        value = base64.b64encode(json_bytes(document))
        if kind == b"tEXt":
            text = keyword + b"\0" + value
        elif kind == b"zTXt":
            text = keyword + b"\0\0" + zlib.compress(value)
        else:
            text = keyword + b"\0\x01\0\0\0" + zlib.compress(value)
        metadata = chunk(kind, text)
    return PNG_SIGNATURE + HEADER + metadata + b"".join(additional) + IMAGE + END


class CharacterCardTests(unittest.TestCase):
    def test_flat_v1_defaults_optional_fields_and_preserves_native_document(self):
        document = {"name": "星星", "description": "喜欢观察星空。"}
        card = parse_card_bytes(json_bytes(document), ".json")
        self.assertEqual(card.name, "星星")
        self.assertEqual(card.spec, "chara_card_v1")
        self.assertEqual(card.personality, "")
        self.assertEqual(card.alternate_greetings, ())
        self.assertEqual(card.to_dict(), document)

    def test_v2_fields_and_placeholder_text_are_retained_as_data(self):
        document = card_document()
        card = CharacterCard.from_dict(document)
        for key in ("name", "description", "personality", "scenario", "first_mes", "mes_example",
                    "system_prompt", "post_history_instructions", "creator_notes"):
            self.assertEqual(getattr(card, key), document["data"][key])
        self.assertEqual(card.alternate_greetings, tuple(document["data"]["alternate_greetings"]))
        self.assertEqual(card.to_dict(), document)

    def test_v3_retains_unknown_fields_lorebook_assets_and_future_version(self):
        document = card_document(version=3)
        document["spec_version"] = "3.1"
        document["future_top"] = {"flag": True}
        document["data"].update(nickname="小星", assets=[{"type": "icon", "uri": "https://example.invalid/avatar.png"}],
            character_book={"entries": [{"keys": ["月亮"], "content": "只是一段设定。", "enabled": True}]},
            future_field=[1, "保存这个字段"])
        card = CharacterCard.from_dict(document)
        self.assertEqual(card.spec, "chara_card_v3")
        self.assertEqual(card.spec_version, "3.1")
        self.assertEqual(card.to_dict(), document)
        self.assertTrue(card.warnings)
        self.assertEqual(card.character_book, document["data"]["character_book"])
        self.assertIn("已保留世界书，当前仅应用基础人设。", card.warnings)

    def test_v3_nickname_is_used_for_prompt_name_and_v2_unknown_nickname_is_preserved(self):
        document = card_document("正式名称", version=3)
        document["data"]["nickname"] = " 小星 "
        card = CharacterCard.from_dict(document)
        self.assertEqual(card.name, "正式名称")
        self.assertEqual(card.nickname, "小星")
        self.assertEqual(card.prompt_name, "小星")
        self.assertEqual(card.to_dict()["data"]["nickname"], " 小星 ")
        document["data"]["nickname"] = " "
        self.assertEqual(CharacterCard.from_dict(document).prompt_name, "正式名称")
        document["spec"], document["spec_version"] = "chara_card_v2", "2.0"
        document["data"]["nickname"] = "其它应用的未知字段"
        legacy = CharacterCard.from_dict(document)
        self.assertEqual(legacy.nickname, "")
        self.assertEqual(legacy.prompt_name, "正式名称")
        self.assertEqual(legacy.to_dict(), document)

    def test_v3_nickname_type_and_size_are_validated(self):
        for value in (None, 1, "x" * 129):
            document = card_document(version=3)
            document["data"]["nickname"] = value
            with self.subTest(value_type=type(value)), self.assertRaises(CharacterCardError):
                CharacterCard.from_dict(document)

    def test_null_optional_lorebook_is_preserved_as_absent(self):
        document = card_document()
        document["data"]["character_book"] = None
        card = CharacterCard.from_dict(document)
        self.assertIsNone(card.character_book)
        self.assertEqual(card.to_dict(), document)
        self.assertNotIn("已保留世界书，当前仅应用基础人设。", card.warnings)

    def test_imported_card_is_detached_from_caller_and_returned_nested_data(self):
        document = card_document()
        document["data"]["character_book"] = {"entries": [{"content": "原始设定"}]}
        card = CharacterCard.from_dict(document)
        document["data"]["personality"] = "外部修改"
        card.to_dict()["data"]["personality"] = "返回值修改"
        card.extensions["example"]["enabled"] = False
        card.character_book["entries"][0]["content"] = "返回值修改"
        self.assertEqual(card.personality, "温柔，偶尔调皮。")
        self.assertTrue(card.extensions["example"]["enabled"])
        self.assertEqual(card.character_book["entries"][0]["content"], "原始设定")
        with self.assertRaises(FrozenInstanceError):
            card.name = "修改名称"

    def test_constructed_basic_card_exports_a_native_v2_document(self):
        original = CharacterCard(name="新的角色", personality="活泼")
        loaded = CharacterCard.from_dict(original.to_dict())
        self.assertEqual(loaded.name, original.name)
        self.assertEqual(loaded.personality, original.personality)

    def test_png_v2_and_v3_are_selected_by_the_embedded_metadata(self):
        for version, keyword in ((2, b"chara"), (3, b"ccv3")):
            with self.subTest(version=version):
                card = parse_card_bytes(png_bytes(card_document(version=version), keyword=keyword), ".png")
                self.assertEqual(card.spec, f"chara_card_v{version}")

    def test_ccv3_has_priority_over_legacy_chara_regardless_of_chunk_order(self):
        older, newer = card_document("旧角色"), card_document("新角色", version=3)
        legacy = chunk(b"tEXt", b"chara\0" + base64.b64encode(json_bytes(older)))
        current = chunk(b"tEXt", b"ccv3\0" + base64.b64encode(json_bytes(newer)))
        for chunks in ((legacy, current), (current, legacy)):
            with self.subTest(order=chunks[0][8:12]):
                self.assertEqual(parse_card_bytes(png_bytes(additional=chunks)).name, "新角色")

    def test_invalid_preferred_v3_is_not_replaced_by_a_different_legacy_character(self):
        invalid = chunk(b"tEXt", b"ccv3\0not-a-valid-base64-value")
        with self.assertRaises(CharacterCardError):
            parse_card_bytes(png_bytes(card_document(), additional=(invalid,)))

    def test_compressed_png_text_and_international_text_are_bounded_and_supported(self):
        for kind in (b"zTXt", b"iTXt"):
            with self.subTest(kind=kind):
                self.assertEqual(parse_card_bytes(png_bytes(card_document(), kind=kind)).name, "测试角色")

    def test_uncompressed_international_text_and_line_wrapped_base64(self):
        encoded = base64.b64encode(json_bytes(card_document()))
        encoded = b"\n".join(encoded[index:index + 60] for index in range(0, len(encoded), 60))
        text = chunk(b"iTXt", b"chara\0\0\0zh\0\0" + encoded)
        self.assertEqual(parse_card_bytes(png_bytes(additional=(text,))).name, "测试角色")

    def test_json_utf8_bom_and_case_insensitive_suffix_are_supported(self):
        self.assertEqual(parse_card_bytes(b"\xef\xbb\xbf" + json_bytes(card_document()), ".JSON").name, "测试角色")
        self.assertEqual(parse_card_bytes(png_bytes(card_document()), "PNG").name, "测试角色")

    def test_load_reads_only_the_explicit_owned_file(self):
        with tempfile.TemporaryDirectory(prefix="aura-card-parser-") as temporary:
            path = Path(temporary) / "角色.json"
            path.write_bytes(json_bytes(card_document()))
            self.assertEqual(load_character_card(path).name, "测试角色")

    def test_plain_avatar_without_card_has_actionable_static_error(self):
        with self.assertRaisesRegex(CharacterCardError, "没有内嵌角色设定"):
            parse_card_bytes(png_bytes(), ".png")

    def test_png_crc_truncation_trailing_bytes_and_missing_structural_chunks_are_rejected(self):
        valid = png_bytes(card_document())
        corrupted = bytearray(valid)
        corrupted[29] ^= 1
        cases = (bytes(corrupted), valid[:-1], valid + b"trailing", valid[:-12],
                 PNG_SIGNATURE + IMAGE + END, PNG_SIGNATURE + HEADER + END,
                 PNG_SIGNATURE + HEADER + HEADER + IMAGE + END,
                 PNG_SIGNATURE + HEADER + IMAGE + chunk(b"IEND", b"x"))
        for data in cases:
            with self.subTest(length=len(data)), self.assertRaises(CharacterCardError):
                parse_card_bytes(data, ".png")

    def test_png_oversized_chunk_length_and_invalid_ihdr_are_rejected(self):
        cases = [PNG_SIGNATURE + struct.pack(">I4s", 0xffffffff, b"tEXt")]
        for values in ((0, 1, 8, 6, 0, 0, 0), (1, 1, 3, 6, 0, 0, 0),
                       (1, 1, 8, 6, 1, 0, 0), (1, 1, 8, 6, 0, 0, 2)):
            cases.append(PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", *values)) + IMAGE + END)
        for data in cases:
            with self.subTest(data=data[:16]), self.assertRaises(CharacterCardError):
                parse_card_bytes(data, ".png")

    def test_ambiguous_duplicate_character_chunks_are_rejected(self):
        extra = chunk(b"tEXt", b"chara\0" + base64.b64encode(json_bytes(card_document("其它角色"))))
        with self.assertRaisesRegex(CharacterCardError, "重复"):
            parse_card_bytes(png_bytes(card_document(), additional=(extra,)))

    def test_png_compression_bomb_and_concatenated_compressed_stream_are_rejected(self):
        too_large = chunk(b"zTXt", b"chara\0\0" + zlib.compress(b"A" * (2 * MAX_CARD_JSON_BYTES)))
        encoded = base64.b64encode(json_bytes(card_document()))
        trailing = chunk(b"zTXt", b"chara\0\0" + zlib.compress(encoded) + zlib.compress(b"extra"))
        for text in (too_large, trailing):
            with self.subTest(length=len(text)), self.assertRaises(CharacterCardError):
                parse_card_bytes(png_bytes(additional=(text,)))

    def test_unknown_compressed_metadata_is_not_decompressed(self):
        unknown = chunk(b"zTXt", b"Comment\0\0not-a-zlib-stream")
        with patch("desktop_pet.character_cards._decompress_text", side_effect=AssertionError("No decompression expected")):
            self.assertEqual(parse_card_bytes(png_bytes(card_document(), additional=(unknown,))).name, "测试角色")

    def test_invalid_compression_flags_and_international_metadata_layout_are_rejected(self):
        chunks = (chunk(b"zTXt", b"chara\0\x01bad"), chunk(b"iTXt", b"chara\0\x02\0\0\0bad"),
                  chunk(b"iTXt", b"chara\0\0\0missing-separators"))
        for text in chunks:
            with self.subTest(length=len(text)), self.assertRaises(CharacterCardError):
                parse_card_bytes(png_bytes(additional=(text,)))

    def test_json_duplicate_keys_nonfinite_numbers_and_non_object_roots_are_rejected(self):
        cases = (b'{"name":"first","name":"second"}', b'{"name":"x","extensions":{"a":1,"a":2}}',
                 b'{"name":"x","score":NaN}', b'{"name":"x","score":Infinity}', b'[]', b'null')
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(CharacterCardError):
                parse_card_bytes(raw)

    def test_core_field_types_names_and_optional_containers_are_validated(self):
        cases = [{}, {"name": " "}, {"name": "x" * 129}, {"name": 1},
                 {"name": "x", "description": None}, {"name": "x", "alternate_greetings": [1]},
                 {"name": "x", "character_book": []}, {"name": "x", "extensions": []},
                 {"spec": "unknown", "data": {"name": "x"}},
                 {"spec": "chara_card_v2", "data": [], "spec_version": "2.0"}]
        for document in cases:
            with self.subTest(document=document), self.assertRaises(CharacterCardError):
                CharacterCard.from_dict(document)

    def test_json_depth_unicode_and_non_json_python_values_are_rejected(self):
        deep = current = {}
        for _ in range(35):
            current["next"] = {}
            current = current["next"]
        cases = ({"name": "x", "nested": deep}, {"name": "x\0"}, {"name": "\ud800"},
                 {"name": "x", "invalid": object()}, {"name": "x", "invalid": float("nan")})
        for document in cases:
            with self.subTest(case=type(document)), self.assertRaises(CharacterCardError):
                CharacterCard.from_dict(document)

    def test_text_limit_is_rejected_without_truncating_the_character(self):
        exact = {"name": "x", "description": "a" * (MAX_CARD_TEXT_CHARACTERS - 1)}
        self.assertEqual(CharacterCard.from_dict(exact).description, exact["description"])
        exact["description"] += "b"
        with self.assertRaisesRegex(CharacterCardError, "24000"):
            CharacterCard.from_dict(exact)
        with self.assertRaises(CharacterCardError):
            CharacterCard.from_dict({"name": "x", "alternate_greetings": ["a" * MAX_CARD_TEXT_CHARACTERS]})

    def test_metadata_json_limit_and_file_limit_are_enforced(self):
        with self.assertRaises(CharacterCardError):
            parse_card_bytes(b" " * (MAX_CARD_JSON_BYTES + 1), ".json")
        with self.assertRaises(CharacterCardError):
            CharacterCard.from_dict({"name": "x", "creator_notes": "a" * MAX_CARD_JSON_BYTES})
        with self.assertRaises(CharacterCardError):
            parse_card_bytes(b"x" * (MAX_CARD_FILE_BYTES + 1), ".png")

    def test_unsupported_format_and_mismatched_suffix_have_static_errors(self):
        for raw, suffix in ((json_bytes(card_document()), ".charx"), (json_bytes(card_document()), ".png"),
                            (png_bytes(card_document()), ".json"), ("text", "json")):
            with self.subTest(suffix=suffix), self.assertRaises(CharacterCardError):
                parse_card_bytes(raw, suffix)

    def test_cards_are_data_and_errors_do_not_echo_private_contents_or_file_paths(self):
        text = '<script>throw new Error("PRIVATE_ROLE_TEXT")</script> {{dangerous_macro}}'
        card = CharacterCard.from_dict({"name": "x", "description": text})
        self.assertEqual(card.description, text)
        for operation in (lambda: parse_card_bytes(b'{"name":"PRIVATE_ROLE_TEXT",broken}'),
                          lambda: load_character_card(Path("PRIVATE_ROLE_TEXT") / "missing.json")):
            with self.assertRaises(CharacterCardError) as caught:
                operation()
            self.assertNotIn("PRIVATE_ROLE_TEXT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

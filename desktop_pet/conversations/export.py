"""Explicit conversation exports; no settings, credentials or image metadata.

Call from a worker. A read-only SQLite transaction snapshots the complete
archive, independently of the chat view and the model's recent-message budget.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata

from .chronology import register_timestamp_order


class ExportError(RuntimeError):
    """Static, user-facing errors without database, provider or secret values."""


FORMATS = {"markdown": ".md", "json": ".json"}


def _text_without_images(value: str) -> str:
    # Chat normally stores only text. If a legacy/user-supplied message embeds
    # an inline image URI, do not turn exporting text into screenshot export.
    text = re.sub(r"data:image/[^\s,\"'<>)]*,[^\s\"'<>)]*(?:[ \t\r\n]+[A-Za-z0-9+/=_-]+)*",
                  "[图片数据未导出]", str(value), flags=re.IGNORECASE)
    # Titles can be truncated before the comma; these headers must not leak
    # either. Whitespace-wrapped ASCII payload is omitted with the image.
    return re.sub(r"data:image(?:/[^\s\"'<>)]*)?", "[图片数据未导出]", text, flags=re.IGNORECASE)


def _clean_snapshot(value):
    if isinstance(value, str):
        return _text_without_images(value)
    if isinstance(value, list):
        return [_clean_snapshot(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_snapshot(item) for key, item in value.items()}
    return value


def _markdown_text(value, *, inline=False) -> str:
    """One renderer for every dynamic Markdown field, including legacy IDs.

    An HTML entity is not reparsed as Markdown image syntax. Encoding every
    exclamation mark avoids the odd/even-backslash escape trap, including in
    summaries and metadata. Inline fields also cannot break code delimiters.
    """
    text = escape(_text_without_images(str(value))).replace("!", "&#33;")
    return " ".join(text.split()).replace("`", "&#96;") if inline else text


def suggested_export_filename(title: str, conversation_id: str, format: str = "markdown") -> str:
    """A Windows-safe suggestion; it never supplies an output directory."""
    suffix = FORMATS.get(format)
    if suffix is None:
        raise ExportError("不支持的导出格式，请选择 Markdown 或 JSON。")
    value = unicodedata.normalize("NFKC", _text_without_images(str(title)))
    value = "".join(character for character in value
                    if ord(character) >= 32 and unicodedata.category(character) != "Cf")
    value = re.sub(r'[<>:"/\\|?*\x7f]', "_", value)
    value = " ".join(value.split()).strip(" .")[:72].strip(" .") or "对话"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
    if value.split(".", 1)[0].upper() in reserved:
        value = "对话-" + value
    identifier = re.sub(r"[^a-zA-Z0-9]", "", str(conversation_id))[:8] or "session"
    return f"{value}-{identifier}{suffix}"


def _read_snapshot(database_path: Path, conversation_id: str) -> dict:
    if (not isinstance(conversation_id, str) or not conversation_id.strip()
            or len(conversation_id) > 200 or any(ord(char) < 32 for char in conversation_id)):
        raise ExportError("要导出的会话标识无效。")
    connection = None
    try:
        # mode=ro neither creates a missing database nor runs migrations.
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        register_timestamp_order(connection)
        connection.execute("BEGIN")
        row = connection.execute("""SELECT id, title, summary, created_at, updated_at, status
            FROM conversations WHERE id = ?""", (conversation_id,)).fetchone()
        if row is None:
            raise ExportError("此会话尚未保存或已删除，没有可导出的消息。")
        conversation = dict(row)
        # Deliberately do not read JSON metadata: it is not a supported export
        # field and must never become a route for credentials or screenshots.
        messages = [dict(row) for row in connection.execute("""SELECT id, conversation_id, role,
            content, created_at, source, screen_event_id FROM messages
            WHERE conversation_id = ? ORDER BY aura_timestamp(created_at) ASC, rowid ASC""", (conversation_id,))]
        if not messages:
            raise ExportError("当前会话没有已保存消息，未创建导出文件。")
        # Export is read-only and also accepts older archives before their next
        # normal application startup performs the source-provenance migration.
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(screen_events)")}
        source_column = "source_kind" if "source_kind" in event_columns else "'screen' AS source_kind"
        events = {row["id"]: dict(row) for row in connection.execute(f"""SELECT id, conversation_id,
            observed_at, app_name, context_summary, event_type, {source_column} FROM screen_events
            WHERE conversation_id = ?""", (conversation_id,))}
        for message in messages:
            if (message["role"], message["source"]) not in {
                    ("user", "user"), ("assistant", "assistant"),
                    ("assistant", "proactive_screen"), ("assistant", "proactive_camera")}:
                raise ExportError("会话消息格式异常，未创建导出文件；原记录保持不变。")
            event_id = message["screen_event_id"]
            visual_source = message["source"] in {"proactive_screen", "proactive_camera"}
            if visual_source != (event_id is not None):
                raise ExportError("会话关联情境不完整，未导出；请保留原记录后重试。")
            if event_id is not None:
                if (event_id not in events
                        or "proactive_" + str(events[event_id]["source_kind"]) != message["source"]):
                    raise ExportError("会话关联情境不完整，未导出；请保留原记录后重试。")
                message["screen_event"] = dict(events[event_id])
        conversation["message_count"] = len(messages)
        return _clean_snapshot({"format": "aura.conversation", "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "conversation": conversation, "messages": messages})
    except (sqlite3.Error, OSError, ValueError):
        raise ExportError("读取完整聊天记录失败，未导出；请检查本地数据库和文件权限。") from None
    finally:
        if connection is not None:
            connection.close()


def _markdown(snapshot: dict) -> str:
    conversation = snapshot["conversation"]
    # Keep message Markdown readable, while preventing raw HTML from becoming
    # active content when the exported document is opened by another viewer.
    lines = [f"# {_markdown_text(conversation['title'], inline=True)}", "",
             f"会话 ID：{_markdown_text(conversation['id'], inline=True)}", "",
             f"创建：{_markdown_text(conversation['created_at'], inline=True)}  ",
             f"更新：{_markdown_text(conversation['updated_at'], inline=True)}  ",
             f"消息数：{conversation['message_count']}", ""]
    if conversation["summary"]:
        lines.extend(["## 会话概况", "", _markdown_text(conversation["summary"]), ""])
    for message in snapshot["messages"]:
        speaker = "你" if message["role"] == "user" else "鲸鱼娘"
        if message["source"] in {"proactive_screen", "proactive_camera"}:
            speaker += " · 来自摄像头感知" if message["source"] == "proactive_camera" else " · 来自屏幕感知"
        lines.extend([f"## {speaker}", "", f"时间：{_markdown_text(message['created_at'], inline=True)}  ",
                      f"来源：{_markdown_text(message['source'], inline=True)} · 消息 ID：{_markdown_text(message['id'], inline=True)}", "",
                      _markdown_text(message["content"]), ""])
        event = message.get("screen_event")
        if event:
            camera = event.get("source_kind", "screen") == "camera"
            event_label, origin_label = ("摄像头", "设备") if camera else ("屏幕", "应用")
            lines.extend([f"> **关联{event_label}情境**", f"> 事件 ID：{_markdown_text(event['id'], inline=True)}  ",
                          f"> {origin_label}：{_markdown_text(event['app_name'], inline=True)}  ",
                          f"> 时间：{_markdown_text(event['observed_at'], inline=True)} · 类型：{_markdown_text(event['event_type'], inline=True)}", ">"])
            lines.extend("> " + _markdown_text(line) for line in str(event["context_summary"]).splitlines())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_conversation_file(database_path: str | Path, conversation_id: str,
                             destination: str | Path, format: str = "markdown") -> dict:
    """Write one complete snapshot with an atomic same-directory replacement."""
    if format not in FORMATS:
        raise ExportError("不支持的导出格式，请选择 Markdown 或 JSON。")
    database_path, destination = Path(database_path), Path(destination)
    if destination.suffix.lower() != FORMATS[format]:
        raise ExportError("导出扩展名与格式不一致，请使用 .md 或 .json。")
    try:
        if destination.resolve() == database_path.resolve():
            raise ExportError("不能将导出文件写入聊天数据库位置。")
    except OSError:
        raise ExportError("无法访问导出位置，请重新选择文件。") from None
    snapshot = _read_snapshot(database_path, conversation_id)
    content = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
               if format == "json" else _markdown(snapshot))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False,
                                         prefix=".aura-export-", suffix=".tmp", dir=destination.parent) as output:
            temporary_path = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except (OSError, ValueError):
        raise ExportError("导出文件写入失败，原对话和已有目标文件均未更改；请检查空间与权限。") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return {"conversation_id": conversation_id, "path": str(destination),
            "message_count": snapshot["conversation"]["message_count"], "format": format}

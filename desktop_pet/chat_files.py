"""Public file attachment metadata, without local paths or executable content."""
import re


def checked_file_metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"file_id", "file_name", "file_size", "file_mime"}:
        raise ValueError("INVALID_FILE_METADATA")
    digest, name, size, mime = (value[key] for key in ("file_id", "file_name", "file_size", "file_mime"))
    if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(name, str) or not 1 <= len(name) <= 160 or name in {".", ".."}
            or any(ord(c) < 32 or c in '/\\<>:"|?*' for c in name)
            or type(size) is not int or not 0 <= size <= 32 * 1024 * 1024
            or not isinstance(mime, str) or len(mime) > 120
            or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)):
        raise ValueError("INVALID_FILE_METADATA")
    return dict(value)

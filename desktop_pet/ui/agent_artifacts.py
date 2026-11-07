"""Bounded public delivery references, never source paths."""
import re


def public_artifacts(value):
    result, seen = [], set()
    if not isinstance(value, list):
        return result
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        reference, name, mime = item.get("reference"), item.get("name"), item.get("mime")
        size, digest, scope = item.get("bytes"), item.get("sha256"), item.get("verification_scope")
        if (not isinstance(reference, str) or not re.fullmatch(r"artifact_[a-f0-9]{64}", reference)
                or reference in seen or not isinstance(name, str) or not 1 <= len(name) <= 240
                or name in {".", ".."} or any(ord(c) < 32 or c in "/\\" for c in name)
                or not isinstance(mime, str) or len(mime) > 120
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)
                or type(size) is not int or not 0 <= size <= 16 * 1024 * 1024
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(scope, str) or len(scope) > 160 or any(ord(c) < 32 for c in scope)):
            continue
        result.append({key: item[key] for key in ("reference", "name", "mime", "bytes", "sha256", "verification_scope")})
        seen.add(reference)
    return result

"""Public assistant commentary, never reasoning or arbitrary tool output."""
import re
import unicodedata
from urllib.parse import urlsplit


def public_commentary(value, redact=lambda text: text):
    if not isinstance(value, str) or not value.strip() or len(value) > 3000:
        return ""
    value = redact(value)
    value = re.sub(r"<(think|thinking|analysis|reasoning)\b[^>]*>.*?(?:</\1>|$)", "", value, flags=re.I | re.S)
    if re.match(r"\s*(thought|analysis|reasoning(?:_content)?)\s*:", value, re.I):
        return ""
    value = re.sub(r"```.*?(?:```|$)", "[代码已省略]", value, flags=re.S)
    value = re.sub(r"data:[^\s<>\"']+", "[内嵌内容已省略]", value, flags=re.I)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[已隐藏密钥]", value)
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", "[已隐藏凭据]", value, flags=re.I)
    value = re.sub(r"[\"']?\b(?:api[_ -]?key|access[_ -]?token|password|passwd|secret)[\"']?\s*[:=]\s*(?:\"[^\"\n]*(?:\"|(?=\n)|$)|'[^'\n]*(?:'|(?=\n)|$)|[^\s,;]+)",
                   "[已隐藏凭据]", value, flags=re.I)
    def website(match):
        try:
            host = urlsplit(match[0]).hostname
            return host if host and re.fullmatch(r"[A-Za-z0-9.-]+", host) else "[网站]"
        except ValueError:
            return "[网站]"
    value = re.sub(r"https?://[^\s<>\"'`]+", website, value, flags=re.I)
    value = re.sub(r"[A-Za-z]:[\\/][^\s<>\"'`]*|\\\\[^\s<>\"'`]+", "[本地文件]", value)
    value = re.sub(r"(?<![\w:])/(?:[^\s/<>\"'`]+/)*[^\s/<>\"'`]+", "[本地文件]", value)
    value = re.sub(r"<[^>]*>", "", value)
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32 and ord(char) != 127 and unicodedata.category(char) != "Cf")
    return value.strip()

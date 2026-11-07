"""Shared pure-Python admission patterns for the host and memory worker."""
import re

_SENSITIVE = re.compile(
    r"(?:api[ _-]?key|password|passcode|secret|bearer\s|sk-[A-Za-z0-9]|密码|验证码|银行卡|身份证|私钥)",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"不再|改成|改为|纠正|更正|更新我的|更新偏好|以前.{0,30}现在|\b(?:no longer|instead|correct my|update my|used to)\b",
    re.IGNORECASE,
)

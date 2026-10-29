"""Small OpenAI-compatible transport with safe, user-facing failures.

This module performs synchronous I/O; the application only calls it from the
worker in ``chat_controller``. No API response is evaluated as executable code.
"""

from dataclasses import dataclass
import asyncio
import json
import logging
import re
import threading

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from .settings_manager import ApiSettings


EMOTIONS = frozenset({"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"})
MAX_REPLY_CHARACTERS = 24_000
SYSTEM_PROMPT = (
    "你是桌面宠物鲸鱼娘，用简短、自然的中文回应用户。"
    "请尽量输出一个 JSON 对象，格式为 "
    '{"reply":"对用户的回答","emotion":"happy"}。'
    "emotion 只能是 neutral、happy、surprised、sleepy、angry、thinking、talking。"
    "如果无法输出 JSON，直接输出普通文本回答也可以。"
    "本阶段只聊天，不声称能观察屏幕、操作电脑或拥有长期记忆。"
)


class ApiError(Exception):
    """Only static, sanitized messages cross the transport/UI boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ChatReply:
    reply: str
    emotion: str = "neutral"


def redact_secret(text: str, api_key: str) -> str:
    return text.replace(api_key, "[已隐藏密钥]") if api_key else text


def parse_reply(raw: str) -> ChatReply:
    """Accept optional JSON fences; invalid structures remain ordinary text."""
    text = raw.strip()
    candidate = text
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.I | re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError, RecursionError):
        return ChatReply(text)
    if not isinstance(value, dict):
        return ChatReply(text)
    reply = value.get("reply")
    emotion = value.get("emotion", "neutral")
    if not isinstance(reply, str) or not reply.strip() or not isinstance(emotion, str) or emotion not in EMOTIONS:
        return ChatReply(text)
    return ChatReply(reply.strip(), emotion)


def _disable_transport_logging() -> None:
    # OPENAI_LOG=debug and HTTPX debug logs may expose bodies. The desktop app
    # does not need transport logging, including errors containing remote bodies.
    packages = ("openai", "httpx", "httpcore")
    names = set(packages) | {
        name for name in logging.Logger.manager.loggerDict
        if any(name.startswith(package + ".") for package in packages)
    }
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True


class ApiClient:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._tasks_lock = threading.Lock()
        self._tasks: set[tuple[asyncio.AbstractEventLoop, asyncio.Task]] = set()

    def shutdown(self) -> None:
        """Cancel HTTP tasks from the GUI thread without waiting for sockets."""
        self._cancelled.set()
        with self._tasks_lock:
            active = list(self._tasks)
        for loop, task in active:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker's event loop has already finished.

    async def _request(self, settings: ApiSettings, api_key: str, messages: list[dict]):
        async with AsyncOpenAI(
            api_key=api_key, base_url=settings.base_url,
            timeout=settings.timeout_seconds, max_retries=0,
        ) as client:
            return await client.chat.completions.create(model=settings.model, messages=messages)

    async def _run_request(self, settings: ApiSettings, api_key: str, messages: list[dict]):
        task = asyncio.create_task(self._request(settings, api_key, messages))
        entry = (asyncio.get_running_loop(), task)
        with self._tasks_lock:
            self._tasks.add(entry)
        try:
            if self._cancelled.is_set():
                task.cancel()
            # SDK timeouts bound individual network operations. Also cap the
            # entire completion so a slowly trickling response cannot run forever.
            return await asyncio.wait_for(task, timeout=settings.timeout_seconds)
        finally:
            with self._tasks_lock:
                self._tasks.discard(entry)

    def complete(self, settings: ApiSettings, api_key: str, messages: list[dict]) -> ChatReply:
        settings.validate(require_model=True)
        if not api_key or not api_key.strip():
            raise ApiError("not_configured", "尚未配置 API Key，请在 API 设置中保存密钥。")
        # Copy the caller's context and only accept supported chat roles/text.
        request_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in messages:
            if item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
                raise ApiError("invalid_messages", "聊天消息格式不正确，请清空当前会话后重试。")
            request_messages.append({"role": item["role"], "content": redact_secret(item["content"], api_key)})
        _disable_transport_logging()
        try:
            if self._cancelled.is_set():
                raise ApiError("cancelled", "请求已取消。")
            completion = asyncio.run(self._run_request(settings, api_key, request_messages))
            if not completion.choices:
                raise ApiError("empty_response", "服务商返回了空回复，请检查模型设置后重试。")
            content = completion.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ApiError("empty_response", "服务商未返回文本回复，请检查模型是否支持聊天。")
            parsed = parse_reply(redact_secret(content, api_key)[:MAX_REPLY_CHARACTERS])
            # JSON escapes (for example \\u0073) are decoded by parse_reply and
            # may reconstruct a secret absent from the raw response text.
            return ChatReply(redact_secret(parsed.reply, api_key), parsed.emotion)
        except (APITimeoutError, TimeoutError, asyncio.TimeoutError):
            raise ApiError("timeout", "请求超时，请检查网络或稍后重试。") from None
        except asyncio.CancelledError:
            raise ApiError("cancelled", "请求已取消。") from None
        except APIConnectionError:
            raise ApiError("network", "网络连接失败，请检查 Base URL、代理、证书和网络连接。") from None
        except APIStatusError as exc:
            status = exc.status_code
            if status == 401:
                error = ApiError("authentication", "API Key 无效或认证失败（HTTP 401），请检查密钥。")
            elif status == 403:
                error = ApiError("permission", "认证或访问权限不足（HTTP 403），请检查密钥和模型权限。")
            elif status == 404:
                error = ApiError("model_not_found", "模型不存在、无权访问，或 Base URL 端点不正确（HTTP 404）。")
            elif status == 429:
                error = ApiError("rate_limit", "请求受到限流或额度不足（HTTP 429），请检查账户额度或稍后重试。")
            else:
                error = ApiError("provider", f"服务商返回错误（HTTP {int(status)}），请检查模型、接口设置或稍后重试。")
            raise error from None
        except ApiError:
            raise
        except (APIError, ValueError, TypeError, AttributeError, IndexError):
            raise ApiError("invalid_response", "服务商响应格式不兼容或请求参数无效，请检查 API 设置。") from None

    def test_connection(self, settings: ApiSettings, api_key: str) -> str:
        # A real completion verifies authentication, model access, and endpoint.
        # Avoid /models: some compatible providers do not expose that endpoint.
        self.complete(settings, api_key, [{"role": "user", "content": "这是连接测试，请只回复：连接成功。"}])
        return "连接成功：已收到聊天接口的实际回复，当前模型可用。"

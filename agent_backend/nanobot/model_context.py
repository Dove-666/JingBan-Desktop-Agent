"""Let the configured model service govern context/output capacity."""
from nanobot.agent.context_governance import ContextGovernor
from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def context_capacity_error(value):
    # Inspect only an SDK error, never user content, and never return its text.
    text = str(value).casefold()
    return any(marker in text for marker in (
        "context_length_exceeded", "context_window_exceeded", "maximum context length",
        "context length exceeded", "context window exceeded", "too many input tokens", "prompt is too long"))


class ModelManagedProvider(OpenAICompatProvider):
    async def _run_with_retry(self, call, kw, original_messages, **kwargs):
        # nanobot 0.4.0 normally retries a non-transient image error after
        # removing every image. A user-supplied image is part of this exact
        # request: preserve transient retries, but never send a text-only
        # replacement that could be mistaken for a successful vision answer.
        if not self._contains_image_content(kw.get("messages")):
            return await super()._run_with_retry(call, kw, original_messages, **kwargs)
        previous = None
        async def preserve_images(**attempt):
            nonlocal previous
            if not self._contains_image_content(attempt.get("messages")):
                return previous
            previous = await call(**attempt)
            return previous
        return await super()._run_with_retry(preserve_images, kw, original_messages, **kwargs)

    def _build_kwargs(self, *args, **kwargs):
        value = super()._build_kwargs(*args, **kwargs)
        # SDK generation uses a numeric zero internally, while omitting these
        # request fields leaves the actual limit to the configured service.
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value.pop(name, None)
        return value


class ModelManagedContext(ContextGovernor):
    @staticmethod
    def apply_tool_result_budget(config, messages):
        return messages

    @staticmethod
    def normalize_tool_result(config, tool_call_id, tool_name, result):
        return result

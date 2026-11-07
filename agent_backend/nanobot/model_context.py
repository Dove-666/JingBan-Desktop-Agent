"""Recoverable request working sets without replaying any tool side effects."""
from __future__ import annotations
from dataclasses import replace
from nanobot.agent.context_governance import ContextGovernor
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.utils.helpers import estimate_prompt_tokens_chain
try:
    from .working_context import compact_messages, project_tool_bodies, request_bytes
except ImportError:
    from working_context import compact_messages, project_tool_bodies, request_bytes


def context_capacity_error(value):
    """Classify actual SDK/provider failures without echoing their private body."""
    values = [value]
    if value is not None:
        values += [getattr(value, key, None) for key in ('error_code', 'content', 'error', 'body')]
    text = ' '.join(str(item).casefold() for item in values if item is not None)
    return any(marker in text for marker in (
        'context_length_exceeded', 'context_window_exceeded', 'max_context_length_exceeded',
        'maximum context length', 'context length exceeded', 'context window exceeded',
        'too many input tokens', 'prompt is too long', 'model input exceeds the local context budget'))


class ModelManagedProvider(OpenAICompatProvider):
    """Public provider entry points, bounded capacity recovery, no image loss."""
    def __init__(self, *args, context_store=None, context_window_tokens=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_store = context_store
        self.context_window_tokens = context_window_tokens if type(context_window_tokens) is int and context_window_tokens > 0 else None
        self.context_events = []
        self._measured_ratio = None
        self._accepted_working_bytes = None

    def _record_usage(self, response, messages):
        usage = getattr(response, 'usage', None)
        count = getattr(usage, 'input_tokens', None) or getattr(usage, 'context_tokens', None)
        if type(count) is int and count > 0:
            self._measured_ratio = request_bytes(messages) / count

    @staticmethod
    def _fresh_context(kwargs):
        value = dict(kwargs)
        context = value.get('provider_context')
        if context is not None:
            try:
                value['provider_context'] = replace(context, conversation_state=None)
            except TypeError:
                value.pop('provider_context', None)
        return value

    async def _governed_call(self, method, messages, kwargs):
        projected = project_tool_bodies(messages, self.context_store)
        if self._accepted_working_bytes is not None:
            projected = compact_messages(projected, self.context_store,
                                         target_bytes=self._accepted_working_bytes)
        if self.context_window_tokens and self._measured_ratio:
            target = int(self.context_window_tokens * self._measured_ratio * 0.70)
            projected = compact_messages(projected, self.context_store, target_bytes=max(4096, target))
        changed = projected != messages
        local_kwargs = self._fresh_context(kwargs) if changed else dict(kwargs)
        for recovery in range(3):
            try:
                response = await method(messages=projected, **local_kwargs)
            except Exception as exc:
                if not context_capacity_error(exc) or recovery == 2:
                    raise
                error, response = exc, None
            else:
                if not context_capacity_error(response) or getattr(response, 'finish_reason', None) != 'error':
                    self._record_usage(response, projected)
                    if recovery:
                        # Learn a proven accepted payload for this provider
                        # session, even when model-window metadata is unknown.
                        self._accepted_working_bytes = request_bytes(projected)
                    if changed and hasattr(response, 'provider_state'):
                        response.provider_state = None
                    return response
                error = None
                if recovery == 2:
                    return response
            smaller = compact_messages(projected, self.context_store,
                target_bytes=max(2048, request_bytes(projected)//2), keep_batches=max(1, 3-recovery))
            if request_bytes(smaller) >= request_bytes(projected):
                if error is not None:
                    raise error
                return response
            self.context_events.append({'kind': 'context_compaction', 'reason': 'provider_capacity',
                'attempt': recovery+1, 'before_bytes': request_bytes(projected),
                'after_bytes': request_bytes(smaller), 'sources_recallable': True})
            del self.context_events[:-64]
            projected, changed = smaller, True
            local_kwargs = self._fresh_context(local_kwargs)
        return response

    async def chat(self, messages, **kwargs):
        return await self._governed_call(super().chat, messages, kwargs)

    async def chat_stream(self, messages, **kwargs):
        return await self._governed_call(super().chat_stream, messages, kwargs)

    async def _run_with_retry(self, call, kw, original_messages, **kwargs):
        # LLMProvider owns this hook, inherited by OpenAICompatProvider. Keep
        # transient retries but intercept its non-transient text-only fallback.
        parent = getattr(super(), '_run_with_retry', None)
        if parent is None:
            return await call(**kw)
        if not self._contains_image_content(kw.get('messages')):
            return await parent(call, kw, original_messages, **kwargs)
        previous = None
        async def preserve_images(**attempt):
            nonlocal previous
            if not self._contains_image_content(attempt.get('messages')):
                return previous
            previous = await call(**attempt)
            return previous
        return await parent(preserve_images, kw, original_messages, **kwargs)

    @staticmethod
    def _output_argument(args, kwargs):
        return kwargs.get('max_tokens', args[3] if len(args)>3 else None)

    def _build_kwargs(self, *args, **kwargs):
        value = super()._build_kwargs(*args, **kwargs)
        if self._output_argument(args, kwargs) == 0:
            for name in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'):
                value.pop(name, None)
        return value

    def _build_responses_body(self, *args, **kwargs):
        value = super()._build_responses_body(*args, **kwargs)
        if self._output_argument(args, kwargs) == 0:
            value.pop('max_output_tokens', None)
        return value


class ModelManagedContext(ContextGovernor):
    """Retain SDK normalization with an accessible archive for every spill."""
    def __init__(self, store=None, *, context_window_tokens=None, working_set_bytes=512_000):
        super().__init__()
        self.store = store
        self.context_window_tokens = context_window_tokens
        self.working_set_bytes = max(32_000, int(working_set_bytes))
        self.context_events = []

    def normalize_tool_result(self, config, tool_call_id, tool_name, result):
        if self.store is None or tool_name in {'read_file', 'recall_context', 'inspect_image'}:
            return result
        maximum = config.max_tool_result_chars if type(config.max_tool_result_chars) is int and config.max_tool_result_chars>0 else 16_000
        message = {'role': 'tool', 'tool_call_id': tool_call_id, 'name': tool_name, 'content': result}
        return project_tool_bodies([message], self.store, maximum_chars=maximum)[0]['content']

    def prepare_for_model(self, config, messages):
        prepared = super().prepare_for_model(config, messages)
        projected = compact_messages(prepared, self.store, target_bytes=self.working_set_bytes)
        if config.context_window_tokens:
            budget = self.input_budget(config)
            for _ in range(3):
                measured, source = estimate_prompt_tokens_chain(config.provider, config.model, projected, config.tools.get_definitions())
                if measured <= budget or measured <= 0:
                    break
                target = int(request_bytes(projected)*max(0.10, budget/measured)*0.80)
                smaller = compact_messages(projected, self.store, target_bytes=max(2048,target), keep_batches=2)
                if request_bytes(smaller) >= request_bytes(projected):
                    break
                self.context_events.append({'kind': 'context_compaction', 'reason': 'request_pressure',
                    'measurement_source': source, 'before_bytes': request_bytes(projected), 'after_bytes': request_bytes(smaller)})
                del self.context_events[:-64]
                projected = smaller
        return projected

    @staticmethod
    def input_budget(config):
        if not config.context_window_tokens:
            return 0
        reserve = config.max_tokens if type(config.max_tokens) is int and config.max_tokens>0 else 4096
        # Preparation space only, not a secretly transmitted generation cap.
        return max(1, config.context_window_tokens-reserve-1024)

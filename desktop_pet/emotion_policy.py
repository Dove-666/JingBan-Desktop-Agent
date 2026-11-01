"""Shared, text-only emotion vocabulary for chat and visual model responses."""


EMOTIONS = frozenset({"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"})

EMOTION_CONTEXT_PROMPT = (
    "按当前对话/画面与自身回应选择表情，不推断用户情绪。"
    "emotion仅可用：neutral正常默认；happy开心鼓励；surprised意外；sleepy困倦休息；"
    "angry仅明确不满，不对低落用户生气；thinking分析；talking普通交流。"
)


def normalize_emotion(value: object) -> str:
    """Keep valid reply text even when a provider omits or invents an emotion."""
    if isinstance(value, str):
        candidate = value.strip().casefold()
        if candidate in EMOTIONS:
            return candidate
    return "neutral"

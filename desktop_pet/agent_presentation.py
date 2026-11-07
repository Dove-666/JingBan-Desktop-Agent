"""Fixed public Agent status and bounded execution events, without private context."""
from datetime import datetime
import re

from .agents.activity import TOOL_LABELS, ACTION_LABELS
from .agents.service import _ERROR_MESSAGES

AGENT_ERRORS = dict(_ERROR_MESSAGES)
AGENT_ACTIVITY = frozenset({
    "正在保存消息…", "正在检索相关记忆…", "正在生成回复…",
    "正在启动任务助手…", "正在规划任务…", "正在使用任务工具…",
    "正在操作电脑…", "正在整理任务结果…", "正在保存回复…",
    *TOOL_LABELS.values(), *ACTION_LABELS.values(),
})

AGENT_TRACE_LABELS = AGENT_ACTIVITY | frozenset({
    "开始处理请求", "回复已完成", "本次请求未能完成", "本次请求已停止",
    "等待你完成当前步骤", "任务未完成", "上次请求意外中断", "任务分派说明（模型公开回复）", "任务分派说明（直接请求）",
})
AGENT_TRACE_STATES = frozenset({"running", "completed", "failed", "cancelled", "needs_user", "interrupted"})
AGENT_TRACE_KINDS = frozenset({"progress", "tool", "computer", "summary"})
AGENT_TRACE_STATUSES = frozenset({"running", "completed", "failed"})
MAX_AGENT_TRACE_EVENTS = 80
MAX_AGENT_TRACE_MS = 31_536_000_000
_TRACE_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")


def _trace_timestamp(value):
    if not isinstance(value, str) or not _TRACE_DATE.fullmatch(value):
        return False
    try:
        parse_value = value
        if value[19] == ".":
            zone_length = 1 if value.endswith("Z") else 6
            parse_value = value[:20] + value[20:-zone_length].ljust(6, "0") + value[-zone_length:]
        parsed = datetime.fromisoformat(parse_value.replace("Z", "+00:00"))
        # ISO's civil timezone range, shared with the portable SQL projection.
        return parsed.tzinfo is not None and abs(parsed.utcoffset().total_seconds()) <= 14 * 3600
    except ValueError:
        return False


def public_agent_trace(value):
    """Project host execution metadata; never copy details, IDs, context or reasoning.

    The archive retains its full trace. Only the mobile view is bounded. IDs
    derive from array positions, so private/worker-supplied identifiers cannot
    leak while the IDs of retained events stay stable during live updates.
    """
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("state"), str) or value["state"] not in AGENT_TRACE_STATES
            or not _trace_timestamp(value.get("started_at"))
            or type(value.get("duration_ms")) is not int
            or not 0 <= value["duration_ms"] <= MAX_AGENT_TRACE_MS
            or not isinstance(value.get("events"), list)):
        return None
    events = []
    for index, event in enumerate(value["events"]):
        if (not isinstance(event, dict) or not isinstance(event.get("kind"), str)
                or event["kind"] not in AGENT_TRACE_KINDS
                or not isinstance(event.get("label"), str) or event["label"] not in AGENT_TRACE_LABELS
                or not isinstance(event.get("status"), str) or event["status"] not in AGENT_TRACE_STATUSES
                or type(event.get("elapsed_ms")) is not int
                or not 0 <= event["elapsed_ms"] <= value["duration_ms"]):
            continue
        events.append({"id": f"step-{index}", "kind": event["kind"], "label": event["label"],
                       "status": event["status"], "elapsed_ms": event["elapsed_ms"]})
    return {"version": 1, "state": value["state"], "started_at": value["started_at"],
            "duration_ms": value["duration_ms"], "events": events[-MAX_AGENT_TRACE_EVENTS:],
            "omitted_count": max(0, len(events) - MAX_AGENT_TRACE_EVENTS)}


def public_agent_trace_sql(metadata, role, mode):
    """Portable JSON1 equivalent of public_agent_trace; no UDF in archive triggers.

    Arguments are trusted internal SQL column expressions. The CASE guards
    malformed legacy JSON before any nested json_extract can execute.
    """
    quoted = lambda values: ",".join("'" + item.replace("'", "''") + "'" for item in sorted(values))
    get = lambda field: f"json_extract(trace, '$.{field}')"
    start, duration = get("started_at"), get("duration_ms")
    # Same strict timestamp syntax as _TRACE_DATE and a normalized local date
    # check: SQLite otherwise accepts impossible dates such as February 30.
    digits = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
    zone_length = f"CASE WHEN substr({start}, -1) = 'Z' THEN 1 ELSE 6 END"
    fraction_length = f"length({start}) - 20 - ({zone_length})"
    timestamp = f"""json_type(trace, '$.started_at') = 'text'
        AND substr({start},1,19) GLOB '{digits}'
        AND CAST(substr({start},1,4) AS INTEGER) BETWEEN 1 AND 9999
        AND date(substr({start},1,10), '+0 days') = substr({start},1,10)
        AND CAST(substr({start},12,2) AS INTEGER) BETWEEN 0 AND 23
        AND CAST(substr({start},15,2) AS INTEGER) BETWEEN 0 AND 59
        AND CAST(substr({start},18,2) AS INTEGER) BETWEEN 0 AND 59
        AND ((substr({start},-1) = 'Z') OR
            (substr({start},-6) GLOB '[-+][0-9][0-9]:[0-9][0-9]'
             AND CAST(substr({start},-5,2) AS INTEGER) BETWEEN 0 AND 14
             AND CAST(substr({start},-2,2) AS INTEGER) BETWEEN 0 AND 59
             AND (CAST(substr({start},-5,2) AS INTEGER) < 14 OR substr({start},-2,2) = '00')))
        AND ((length({start}) = 19 + ({zone_length})) OR
             (substr({start},20,1) = '.' AND ({fraction_length}) BETWEEN 1 AND 6
              AND substr({start},21,({fraction_length})) NOT GLOB '*[^0-9]*'))"""
    event_valid = f"""CASE WHEN type = 'object' THEN
        json_extract(value,'$.kind') IN ({quoted(AGENT_TRACE_KINDS)})
        AND json_extract(value,'$.label') IN ({quoted(AGENT_TRACE_LABELS)})
        AND json_extract(value,'$.status') IN ({quoted(AGENT_TRACE_STATUSES)})
        AND json_type(value,'$.elapsed_ms') = 'integer'
        AND typeof(json_extract(value,'$.elapsed_ms')) = 'integer'
        AND json_extract(value,'$.elapsed_ms') BETWEEN 0 AND {duration}
        ELSE 0 END"""
    events = f"""(SELECT json_group_array(json(safe_event)) FROM
        (SELECT ordinal, json_object('id','step-' || ordinal,
            'kind',json_extract(value,'$.kind'), 'label',json_extract(value,'$.label'),
            'status',json_extract(value,'$.status'), 'elapsed_ms',json_extract(value,'$.elapsed_ms')) AS safe_event
         FROM (SELECT CAST(key AS INTEGER) AS ordinal,value FROM json_each(trace,'$.events')
               WHERE {event_valid} ORDER BY CAST(key AS INTEGER) DESC LIMIT {MAX_AGENT_TRACE_EVENTS})
         ORDER BY ordinal))"""
    omitted = f"""(SELECT max(0,count(*) - {MAX_AGENT_TRACE_EVENTS})
        FROM json_each(trace,'$.events') WHERE {event_valid})"""
    return f"""CASE WHEN json_valid({metadata}) THEN CASE WHEN {role} = 'user'
        AND {mode} = 'agent' AND json_type({metadata}, '$.execution') = 'object' THEN
        (SELECT CASE WHEN json_type(trace,'$.version') = 'integer' AND {get('version')} = 1
            AND {get('state')} IN ({quoted(AGENT_TRACE_STATES)}) AND ({timestamp})
            AND json_type(trace,'$.duration_ms') = 'integer' AND typeof({duration}) = 'integer'
            AND {duration} BETWEEN 0 AND {MAX_AGENT_TRACE_MS}
            AND json_type(trace,'$.events') = 'array' THEN
            json_object('version',1,'state',{get('state')},'started_at',{start},
                'duration_ms',{duration},'events',json({events}),'omitted_count',{omitted}) END
         FROM (SELECT json_extract({metadata}, '$.execution') AS trace)) END END"""

def public_agent_metadata(message):
    metadata = message.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    mode = message.get("execution_mode", metadata.get("execution_mode"))
    code = message.get("agent_error_code", metadata.get("agent_error_code"))
    status = message.get("request_status", metadata.get("request_status"))
    code = code if isinstance(code, str) and code in AGENT_ERRORS and status in {"failed", "cancelled", "interrupted"} else None
    activity = message.get("agent_activity")
    if not isinstance(activity, str) or activity not in AGENT_ACTIVITY:
        activity = None
        execution = metadata.get("execution", {})
        for event in execution.get("events", []) if isinstance(execution, dict) else []:
            if isinstance(event, dict) and isinstance(event.get("label"), str) and event["label"] in AGENT_ACTIVITY:
                activity = event["label"]
    trace = (public_agent_trace(metadata.get("execution"))
             if message.get("role") == "user" and mode == "agent" else None)
    return {"execution_mode": "agent" if mode == "agent" else None,
            "agent_error_code": code, "agent_error": AGENT_ERRORS.get(code),
            "agent_activity": activity if status == "pending" else None, "agent_trace": trace}

"""Local Hermes Langfuse plugin.

Operational mode sends the Slack/user prompt and final assistant response to
Langfuse trace input/output. Tool output and file contents stay summarized by
default; credential-like secrets are redacted before transport.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
for _logger_name in (
    "opentelemetry.sdk._shared_internal",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
):
    logging.getLogger(_logger_name).setLevel(logging.CRITICAL)

try:
    from langfuse import Langfuse, propagate_attributes
except Exception:  # pragma: no cover - optional dependency, fail open
    Langfuse = None  # type: ignore[assignment]
    propagate_attributes = None  # type: ignore[assignment]


@dataclass
class TurnState:
    task_id: str
    session_id: str
    user_id: str
    root_ctx: Any
    root_span: Any
    started_at: float
    metadata: Dict[str, Any]
    input_value: Any = None
    output_value: Any = None
    generations: Dict[str, Any] = field(default_factory=dict)
    tools: Dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


_LOCK = threading.Lock()
_CLIENT: Any = None
_INIT_FAILED = object()
_TURNS: dict[str, TurnState] = {}
_SESSION_ATTRS: dict[str, dict[str, str]] = {}

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|authorization)"
        r"\s*[:=]\s*['\"]?([^'\"\s,}]+)"
    ),
    re.compile(r"(?i)(://[^:/\s]+):([^@\s]+)@"),
]
_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|authorization|credential)")
_CONTENT_KEYS = {
    "content",
    "contents",
    "file_content",
    "file_contents",
    "old_str",
    "new_str",
    "patch",
    "diff",
    "body",
    "html",
    "markdown",
    "base64_content",
    "image",
    "bytes",
}
_ERROR_KEYS = {"error", "message", "stderr", "stdout", "traceback", "exception"}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def _env_bool(*names: str, default: bool = False) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _env_int(*names: str, default: int) -> int:
    for name in names:
        value = os.environ.get(name)
        if value:
            try:
                return int(value.strip())
            except Exception:
                return default
    return default


def _capture_content() -> bool:
    mode = _env("LANGFUSE_CAPTURE_CONTENT", "HERMES_LANGFUSE_CAPTURE_CONTENT", default="metadata")
    return mode.lower() not in {"", "0", "false", "off", "metadata", "none"}


def _max_chars() -> int:
    return _env_int("LANGFUSE_MAX_CHARS", "HERMES_LANGFUSE_MAX_CHARS", default=12000)


def _tool_error_max_chars() -> int:
    return _env_int("LANGFUSE_TOOL_ERROR_MAX_CHARS", "HERMES_LANGFUSE_TOOL_ERROR_MAX_CHARS", default=1200)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_attr(value: Any, *, max_chars: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _redact_text(text)
    text = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in text)
    return text[:max_chars]


def _clean_metadata_attr(value: Any, *, max_chars: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _safe_text(text, max_chars=max_chars)


def _first_attr(kwargs: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _clean_attr(kwargs.get(name))
        if value:
            return value
    return ""


def _first_metadata_attr(kwargs: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _clean_metadata_attr(kwargs.get(name))
        if value:
            return value
    return ""


def _session_attr_key(task_id: str = "", session_id: str = "") -> str:
    if task_id:
        return f"task:{task_id}"
    if session_id:
        return f"session:{session_id}"
    return f"thread:{threading.get_ident()}"


def _langfuse_user_id(platform: str, raw_user_id: str, team_id: str = "") -> str:
    raw_user_id = _clean_attr(raw_user_id)
    team_id = _clean_attr(team_id)
    platform = _clean_attr(platform).lower()
    if not raw_user_id:
        return ""
    if platform == "slack":
        return f"slack:{team_id}:{raw_user_id}" if team_id else f"slack:{raw_user_id}"
    if platform == "cron":
        return raw_user_id if raw_user_id.startswith("cron:") else f"cron:{raw_user_id}"
    return f"{platform}:{raw_user_id}" if platform else raw_user_id


def _collect_user_attrs(**kwargs: Any) -> dict[str, str]:
    platform = _first_attr(kwargs, "platform")
    raw_user_id = _first_attr(
        kwargs,
        "cron_job_id",
        "sender_id",
        "user_id",
        "slack_user_id",
        "slack_sender_id",
    )
    team_id = _first_attr(kwargs, "slack_team_id", "team_id", "workspace_id")
    user_name = _first_metadata_attr(
        kwargs,
        "cron_job_name",
        "sender_name",
        "user_name",
        "slack_user_name",
        "slack_sender_name",
    )
    cron_schedule = _first_metadata_attr(
        kwargs,
        "cron_schedule",
        "schedule_display",
        "schedule",
    )
    channel_id = _first_attr(
        kwargs,
        "slack_channel_id",
        "channel_id",
        "chat_id",
    )
    channel_type = _first_attr(
        kwargs,
        "slack_channel_type",
        "channel_type",
        "chat_type",
    )
    thread_ts = _first_attr(
        kwargs,
        "slack_thread_ts",
        "thread_ts",
        "thread_id",
    )
    origin_platform = _first_attr(kwargs, "origin_platform")
    origin_chat_id = _first_attr(kwargs, "origin_chat_id")
    origin_thread_id = _first_attr(kwargs, "origin_thread_id")
    attrs: dict[str, str] = {}
    langfuse_user_id = _langfuse_user_id(platform, raw_user_id, team_id)
    if langfuse_user_id:
        attrs["langfuse_user_id"] = langfuse_user_id
    if platform == "cron":
        if raw_user_id:
            attrs["cron_job_id"] = raw_user_id
        if user_name:
            attrs["cron_job_name"] = user_name
        if cron_schedule:
            attrs["cron_schedule"] = cron_schedule
        if origin_platform:
            attrs["origin_platform"] = origin_platform
        if origin_chat_id:
            attrs["origin_chat_id"] = origin_chat_id
        if origin_thread_id:
            attrs["origin_thread_id"] = origin_thread_id
    elif platform == "slack":
        if raw_user_id:
            attrs["slack_user_id"] = raw_user_id
        if user_name:
            attrs["slack_user_name"] = user_name
        if team_id:
            attrs["slack_team_id"] = team_id
        if channel_id:
            attrs["slack_channel_id"] = channel_id
        if channel_type:
            attrs["slack_channel_type"] = channel_type
        if thread_ts:
            attrs["slack_thread_ts"] = thread_ts
    elif raw_user_id:
        attrs["platform_user_id"] = raw_user_id
        if user_name:
            attrs["platform_user_name"] = user_name
    return attrs


def _remember_user_attrs(*, task_id: str = "", session_id: str = "", **kwargs: Any) -> dict[str, str]:
    attrs = _collect_user_attrs(**kwargs)
    if not attrs:
        return {}
    key = _session_attr_key(task_id, session_id)
    with _LOCK:
        existing = _SESSION_ATTRS.get(key, {})
        merged = {**existing, **{k: v for k, v in attrs.items() if v}}
        _SESSION_ATTRS[key] = merged
        return dict(merged)


def _get_user_attrs(task_id: str = "", session_id: str = "") -> dict[str, str]:
    keys = [_session_attr_key(task_id, session_id)]
    if task_id:
        keys.append(_session_attr_key("", session_id))
    with _LOCK:
        for key in keys:
            attrs = _SESSION_ATTRS.get(key)
            if attrs:
                return dict(attrs)
    return {}


def _forget_user_attrs(task_id: str = "", session_id: str = "") -> None:
    keys = {_session_attr_key(task_id, session_id), _session_attr_key("", session_id)}
    with _LOCK:
        for key in keys:
            _SESSION_ATTRS.pop(key, None)


def _truncate_text(value: str, max_chars: Optional[int] = None) -> str:
    max_chars = max_chars if max_chars is not None else _max_chars()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(://"):
            redacted = pattern.sub(r"\1:<redacted>@", redacted)
        elif "api[_-]?key" in pattern.pattern:
            redacted = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    return redacted


def _safe_text(value: Any, max_chars: Optional[int] = None) -> str:
    return _truncate_text(_redact_text(str(value)), max_chars=max_chars)


def _maybe_parse_json_string(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        parsed, idx = json.JSONDecoder().raw_decode(stripped)
    except Exception:
        return value
    if stripped[idx:].strip():
        return value
    return parsed


def _safe_value(
    value: Any,
    *,
    max_chars: Optional[int] = None,
    depth: int = 0,
    include_content: bool = True,
    parse_json_strings: bool = False,
) -> Any:
    if depth > 4:
        return "<max-depth>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "len": len(value)}
    if isinstance(value, str):
        if parse_json_strings:
            parsed = _maybe_parse_json_string(value)
            if parsed is not value:
                return _safe_value(
                    parsed,
                    max_chars=max_chars,
                    depth=depth,
                    include_content=include_content,
                    parse_json_strings=True,
                )
        return _safe_text(value, max_chars=max_chars)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            key_str = str(key)
            key_lc = key_str.lower()
            if _SENSITIVE_KEY_RE.search(key_str):
                safe[key_str] = "<redacted>"
            elif not include_content and key_lc in _CONTENT_KEYS:
                safe[key_str] = {
                    "omitted": True,
                    "chars": len(str(item)) if item is not None else 0,
                }
            else:
                safe[key_str] = _safe_value(
                    item,
                    max_chars=max_chars,
                    depth=depth + 1,
                    include_content=include_content,
                    parse_json_strings=parse_json_strings,
                )
        return safe
    if isinstance(value, (list, tuple, set)):
        return [
            _safe_value(
                item,
                max_chars=max_chars,
                depth=depth + 1,
                include_content=include_content,
                parse_json_strings=parse_json_strings,
            )
            for item in list(value)[:80]
        ]
    if hasattr(value, "__dict__"):
        return _safe_value(
            vars(value),
            max_chars=max_chars,
            depth=depth + 1,
            include_content=include_content,
            parse_json_strings=parse_json_strings,
        )
    return _safe_text(repr(value), max_chars=max_chars)


def _trace_input_from_user_message(user_message: Any) -> Any:
    if not _capture_content() or user_message in (None, ""):
        return None
    return {"role": "user", "content": _safe_value(user_message, include_content=True)}


def _trace_output_from_assistant_response(assistant_response: Any) -> Any:
    if not _capture_content() or assistant_response in (None, ""):
        return None
    return {"role": "assistant", "content": _safe_value(assistant_response, include_content=True)}


def _extract_last_user_message(messages: Any) -> Any:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _trace_input_from_user_message(message.get("content"))
    return None


def _request_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("message_count", "tool_count", "approx_input_tokens", "request_char_count", "max_tokens"):
        if key in kwargs and kwargs.get(key) is not None:
            summary[key] = kwargs.get(key)
    return summary


def _usage_details(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input") or 0)
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("output")
        or 0
    )
    details: dict[str, int] = {"input": input_tokens, "output": output_tokens}
    cache_read = int(usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    if cache_write:
        details["cache_creation_input_tokens"] = cache_write
    if reasoning:
        details["reasoning_tokens"] = reasoning
    return details


def _assistant_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "content_chars": int(kwargs.get("assistant_content_chars") or 0),
        "tool_call_count": int(kwargs.get("assistant_tool_call_count") or 0),
        "finish_reason": str(kwargs.get("finish_reason") or ""),
    }


def _summarize_tool_result(result: Any, *, success: bool) -> Any:
    parsed = _parse_result(result)
    if isinstance(parsed, dict):
        summary: dict[str, Any] = {
            "success": success,
            "type": "object",
            "keys": [str(k) for k in list(parsed.keys())[:30]],
        }
        for key in ("returncode", "status", "success"):
            if key in parsed:
                summary[key] = parsed.get(key)
        if not success:
            errors: dict[str, Any] = {}
            for key, value in parsed.items():
                if str(key).lower() in _ERROR_KEYS and value:
                    errors[str(key)] = _safe_value(
                        value,
                        max_chars=_tool_error_max_chars(),
                        include_content=False,
                        parse_json_strings=True,
                    )
            if errors:
                summary["error_preview"] = errors
        return summary
    if isinstance(parsed, str):
        summary = {"success": success, "type": "text", "chars": len(parsed)}
        if not success and parsed:
            summary["error_preview"] = _safe_text(parsed, max_chars=_tool_error_max_chars())
        return summary
    if isinstance(parsed, list):
        return {"success": success, "type": "list", "items": len(parsed)}
    return {"success": success, "type": type(parsed).__name__}


def _set_trace_io(root_span: Any, *, input_value: Any = None, output_value: Any = None) -> None:
    try:
        kwargs: dict[str, Any] = {}
        if input_value is not None:
            kwargs["input"] = input_value
        if output_value is not None:
            kwargs["output"] = output_value
        if kwargs:
            root_span.set_trace_io(**kwargs)
    except Exception:
        pass
    try:
        kwargs = {}
        if input_value is not None:
            kwargs["input"] = input_value
        if output_value is not None:
            kwargs["output"] = output_value
        if kwargs:
            root_span.update(**kwargs)
    except Exception:
        pass


def _client() -> Optional[Langfuse]:
    global _CLIENT
    if _CLIENT is _INIT_FAILED:
        return None
    if _CLIENT is not None:
        return _CLIENT
    if Langfuse is None:
        _CLIENT = _INIT_FAILED
        return None

    public_key = _env("LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_PUBLIC_KEY")
    secret_key = _env("LANGFUSE_SECRET_KEY", "HERMES_LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        _CLIENT = _INIT_FAILED
        return None

    try:
        _CLIENT = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=_env(
                "LANGFUSE_BASE_URL",
                "HERMES_LANGFUSE_BASE_URL",
                default="https://cloud.langfuse.com",
            ),
            environment=_env("LANGFUSE_ENV", "HERMES_LANGFUSE_ENV", default="local"),
        )
    except Exception as exc:  # pragma: no cover - fail open
        logger.warning("Langfuse PoC client initialization failed: %s", exc)
        _CLIENT = _INIT_FAILED
        return None
    return _CLIENT


def _turn_key(task_id: str, session_id: str) -> str:
    if task_id:
        return task_id
    if session_id:
        return f"session:{session_id}"
    return f"thread:{threading.get_ident()}"


def _request_key(api_call_count: Any) -> str:
    return str(api_call_count or 0)


def _tool_key(tool_name: str, tool_call_id: str) -> str:
    return tool_call_id or f"{tool_name}:{time.time_ns()}"


def _parse_result(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except Exception:
            return result
    return result


def _tool_success(result: Any) -> bool:
    parsed = _parse_result(result)
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return False
        if parsed.get("success") is False:
            return False
        if parsed.get("status") in {"error", "failed", "failure"}:
            return False
        returncode = parsed.get("returncode")
        if isinstance(returncode, int) and returncode != 0:
            return False
    return True


def _duration_ms(seconds: Any = None, milliseconds: Any = None) -> Optional[int]:
    try:
        if milliseconds is not None:
            return max(0, int(float(milliseconds)))
        if seconds is not None:
            return max(0, int(float(seconds) * 1000))
    except Exception:
        return None
    return None


def _start_turn(
    *,
    task_id: str,
    session_id: str,
    platform: str,
    provider: str,
    model: str,
    api_mode: str,
    input_value: Any = None,
    user_attrs: Optional[dict[str, str]] = None,
) -> Optional[TurnState]:
    client = _client()
    if client is None:
        return None

    started_at = time.time()
    user_attrs = user_attrs or {}
    langfuse_user_id = user_attrs.get("langfuse_user_id", "")
    metadata: Dict[str, Any] = {
        "session_id": session_id,
        "platform": platform,
        "provider": provider,
        "model": model,
        "api_mode": api_mode,
        "turn_started_at": _now_iso(),
    }
    metadata.update(user_attrs)

    try:
        if propagate_attributes is not None:
            attr_ctx = propagate_attributes(
                user_id=langfuse_user_id or None,
                session_id=session_id or task_id or "sessionless",
                trace_name="hermes.turn",
                tags=["hermes", "local-poc"],
            )
        else:
            attr_ctx = None

        if attr_ctx is not None:
            with attr_ctx:
                kwargs: dict[str, Any] = {
                    "name": "hermes.turn",
                    "as_type": "chain",
                    "metadata": metadata,
                    "end_on_exit": False,
                }
                if input_value is not None:
                    kwargs["input"] = input_value
                root_ctx = client.start_as_current_observation(
                    **kwargs,
                )
                root_span = root_ctx.__enter__()
        else:
            kwargs = {
                "name": "hermes.turn",
                "as_type": "chain",
                "metadata": metadata,
                "end_on_exit": False,
            }
            if input_value is not None:
                kwargs["input"] = input_value
            root_ctx = client.start_as_current_observation(
                **kwargs,
            )
            root_span = root_ctx.__enter__()
        _set_trace_io(root_span, input_value=input_value)
        return TurnState(
            task_id=task_id,
            session_id=session_id,
            user_id=langfuse_user_id,
            root_ctx=root_ctx,
            root_span=root_span,
            started_at=started_at,
            metadata=metadata,
            input_value=input_value,
        )
    except Exception as exc:  # pragma: no cover - fail open
        logger.debug("Langfuse PoC start turn failed: %s", exc)
        return None


def _get_or_start_turn(
    *,
    task_id: str,
    session_id: str,
    platform: str,
    provider: str,
    model: str,
    api_mode: str,
    input_value: Any = None,
    user_attrs: Optional[dict[str, str]] = None,
) -> Optional[TurnState]:
    key = _turn_key(task_id, session_id)
    with _LOCK:
        state = _TURNS.get(key)
        if state is None:
            state = _start_turn(
                task_id=task_id,
                session_id=session_id,
                platform=platform,
                provider=provider,
                model=model,
                api_mode=api_mode,
                input_value=input_value,
                user_attrs=user_attrs,
            )
            if state is not None:
                _TURNS[key] = state
        return state


def _find_turn_state(task_id: str, session_id: str) -> Optional[TurnState]:
    key = _turn_key(task_id, session_id)
    state = _TURNS.get(key)
    if state is not None:
        return state
    if session_id:
        for candidate in _TURNS.values():
            if candidate.session_id == session_id:
                return candidate
    return None


def _pop_turn_state(task_id: str, session_id: str) -> Optional[TurnState]:
    key = _turn_key(task_id, session_id)
    state = _TURNS.pop(key, None)
    if state is not None:
        return state
    if session_id:
        for candidate_key, candidate in list(_TURNS.items()):
            if candidate.session_id == session_id:
                return _TURNS.pop(candidate_key, None)
    return None


def _end_observation(
    observation: Any,
    metadata: Optional[dict[str, Any]] = None,
    *,
    output: Any = None,
    usage_details: Optional[dict[str, int]] = None,
) -> None:
    if observation is None:
        return
    try:
        update_kwargs: dict[str, Any] = {}
        if metadata:
            update_kwargs["metadata"] = metadata
        if output is not None:
            update_kwargs["output"] = output
        if usage_details:
            update_kwargs["usage_details"] = usage_details
        if update_kwargs:
            observation.update(**update_kwargs)
        observation.end()
    except Exception as exc:  # pragma: no cover - fail open
        logger.debug("Langfuse PoC end observation failed: %s", exc)


def _finish_turn(
    task_id: str,
    session_id: str,
    *,
    input_value: Any = None,
    output_value: Any = None,
    final_metadata: Optional[dict[str, Any]] = None,
) -> None:
    with _LOCK:
        state = _pop_turn_state(task_id, session_id)
    if state is None:
        return

    if input_value is not None:
        state.input_value = input_value
    if output_value is not None:
        state.output_value = output_value

    ended_at = _now_iso()
    latency = _duration_ms(seconds=time.time() - state.started_at)
    metadata = {
        **state.metadata,
        **(final_metadata or {}),
        "turn_ended_at": ended_at,
        "turn_latency_ms": latency,
        "tool_calls": state.tool_calls,
    }
    try:
        for generation in list(state.generations.values()):
            _end_observation(generation, {"success": False, "ended_by": "turn_finish"})
        for tool in list(state.tools.values()):
            _end_observation(tool, {"success": False, "ended_by": "turn_finish"})
        _set_trace_io(
            state.root_span,
            input_value=state.input_value,
            output_value=state.output_value,
        )
        update_kwargs: dict[str, Any] = {"metadata": metadata}
        if state.input_value is not None:
            update_kwargs["input"] = state.input_value
        if state.output_value is not None:
            update_kwargs["output"] = state.output_value
        state.root_span.update(**update_kwargs)
        state.root_span.end()
    except Exception as exc:  # pragma: no cover - fail open
        logger.debug("Langfuse PoC finish turn failed: %s", exc)
    finally:
        try:
            state.root_ctx.__exit__(None, None, None)
        except Exception:
            pass
        _forget_user_attrs(task_id, session_id)


def _pre_api_request(**kwargs: Any) -> None:
    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    attr_kwargs = {k: v for k, v in kwargs.items() if k not in {"task_id", "session_id"}}
    user_attrs = _remember_user_attrs(task_id=task_id, session_id=session_id, **attr_kwargs)
    if not user_attrs:
        user_attrs = _get_user_attrs(task_id, session_id)
    messages = kwargs.get("messages")
    input_value = _extract_last_user_message(messages)
    state = _get_or_start_turn(
        task_id=task_id,
        session_id=session_id,
        platform=str(kwargs.get("platform") or ""),
        provider=str(kwargs.get("provider") or ""),
        model=str(kwargs.get("model") or ""),
        api_mode=str(kwargs.get("api_mode") or ""),
        input_value=input_value,
        user_attrs=user_attrs,
    )
    if state is None:
        return
    if input_value is not None and state.input_value is None:
        state.input_value = input_value
        _set_trace_io(state.root_span, input_value=input_value)

    api_call_count = kwargs.get("api_call_count")
    req_key = _request_key(api_call_count)
    request_summary = _request_summary(kwargs)
    with _LOCK:
        previous = state.generations.pop(req_key, None)
        if previous is not None:
            _end_observation(previous, {"success": False, "ended_by": "replacement"})
        try:
            obs_kwargs: dict[str, Any] = {
                "name": "llm.call",
                "as_type": "generation",
                "model": str(kwargs.get("model") or ""),
                "metadata": {
                    "provider": str(kwargs.get("provider") or ""),
                    "platform": str(kwargs.get("platform") or ""),
                    "api_mode": str(kwargs.get("api_mode") or ""),
                    "api_call_count": api_call_count,
                    **request_summary,
                },
            }
            if request_summary:
                obs_kwargs["input"] = request_summary
            state.generations[req_key] = state.root_span.start_observation(**obs_kwargs)
        except Exception as exc:  # pragma: no cover - fail open
            logger.debug("Langfuse PoC start LLM observation failed: %s", exc)


def _post_api_request(**kwargs: Any) -> None:
    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    key = _turn_key(task_id, session_id)
    req_key = _request_key(kwargs.get("api_call_count"))

    with _LOCK:
        state = _TURNS.get(key)
        generation = state.generations.pop(req_key, None) if state else None
    if state is None or generation is None:
        return

    latency_ms = _duration_ms(seconds=kwargs.get("api_duration"))
    metadata = {
        "success": True,
        "latency_ms": latency_ms,
        "finish_reason": str(kwargs.get("finish_reason") or ""),
        "assistant_tool_call_count": int(kwargs.get("assistant_tool_call_count") or 0),
        "assistant_content_chars": int(kwargs.get("assistant_content_chars") or 0),
    }
    _end_observation(
        generation,
        metadata,
        output=_assistant_summary(kwargs),
        usage_details=_usage_details(kwargs.get("usage")),
    )


def _pre_llm_call(**kwargs: Any) -> None:
    # Current Hermes versions use pre_api_request/post_api_request for request
    # tracing. Only handle legacy request-shaped pre_llm_call hooks.
    attr_kwargs = {k: v for k, v in kwargs.items() if k not in {"task_id", "session_id"}}
    _remember_user_attrs(
        task_id=str(kwargs.get("task_id") or ""),
        session_id=str(kwargs.get("session_id") or ""),
        **attr_kwargs,
    )
    if not isinstance(kwargs.get("messages"), list):
        return
    _pre_api_request(**kwargs)


def _post_llm_call(**kwargs: Any) -> None:
    if "assistant_response" not in kwargs and "user_message" not in kwargs:
        _post_api_request(**kwargs)
        return

    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    input_value = _trace_input_from_user_message(kwargs.get("user_message"))
    output_value = _trace_output_from_assistant_response(kwargs.get("assistant_response"))
    attr_kwargs = {k: v for k, v in kwargs.items() if k not in {"task_id", "session_id"}}
    user_attrs = _remember_user_attrs(task_id=task_id, session_id=session_id, **attr_kwargs)
    if not user_attrs:
        user_attrs = _get_user_attrs(task_id, session_id)

    state = _find_turn_state(task_id, session_id)
    if state is None:
        state = _get_or_start_turn(
            task_id=task_id,
            session_id=session_id,
            platform=str(kwargs.get("platform") or ""),
            provider=str(kwargs.get("provider") or ""),
            model=str(kwargs.get("model") or ""),
            api_mode=str(kwargs.get("api_mode") or ""),
            input_value=input_value,
            user_attrs=user_attrs,
        )
    if state is None:
        return
    if user_attrs:
        state.metadata.update(user_attrs)
        if user_attrs.get("langfuse_user_id") and not state.user_id:
            state.user_id = user_attrs["langfuse_user_id"]

    metadata = {
        "conversation_history_count": (
            len(kwargs.get("conversation_history"))
            if isinstance(kwargs.get("conversation_history"), list)
            else None
        ),
        "assistant_response_chars": len(str(kwargs.get("assistant_response") or "")),
        "capture_content": _capture_content(),
    }
    _finish_turn(
        task_id,
        session_id,
        input_value=input_value,
        output_value=output_value,
        final_metadata=metadata,
    )


def _pre_tool_call(**kwargs: Any) -> None:
    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    call_id = str(kwargs.get("tool_call_id") or "")
    key = _turn_key(task_id, session_id)
    tkey = _tool_key(tool_name, call_id)

    with _LOCK:
        state = _TURNS.get(key)
        if state is None:
            return
        try:
            state.tools[tkey] = state.root_span.start_observation(
                name=f"tool.{tool_name or 'unknown'}",
                as_type="tool",
                input=_safe_value(
                    kwargs.get("args"),
                    include_content=False,
                    parse_json_strings=True,
                ),
                metadata={
                    "tool_name": tool_name,
                    "tool_call_id": call_id,
                },
            )
        except Exception as exc:  # pragma: no cover - fail open
            logger.debug("Langfuse PoC start tool observation failed: %s", exc)


def _post_tool_call(**kwargs: Any) -> None:
    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    call_id = str(kwargs.get("tool_call_id") or "")
    key = _turn_key(task_id, session_id)
    explicit_key = _tool_key(tool_name, call_id) if call_id else ""

    with _LOCK:
        state = _TURNS.get(key)
        if state is None:
            return
        observation = None
        if explicit_key:
            observation = state.tools.pop(explicit_key, None)
        if observation is None and state.tools:
            # Hermes versions differ on whether pre_tool_call already carries
            # tool_call_id. Fall back to the oldest open tool observation.
            observation = state.tools.pop(next(iter(state.tools)), None)

    success = _tool_success(kwargs.get("result"))
    latency_ms = _duration_ms(milliseconds=kwargs.get("duration_ms"))
    metadata = {
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "success": success,
        "latency_ms": latency_ms,
        "args": _safe_value(
            kwargs.get("args"),
            include_content=False,
            parse_json_strings=True,
        ),
    }
    if state is not None:
        state.tool_calls.append(metadata)
    _end_observation(
        observation,
        metadata,
        output=_summarize_tool_result(kwargs.get("result"), success=success),
    )


def _wrap(callback):
    def _inner(**kwargs: Any) -> None:
        try:
            callback(**kwargs)
        except Exception as exc:  # pragma: no cover - fail open
            logger.debug("Langfuse PoC hook failed: %s", exc)

    return _inner


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", _wrap(_pre_api_request))
    ctx.register_hook("post_api_request", _wrap(_post_api_request))
    ctx.register_hook("pre_llm_call", _wrap(_pre_llm_call))
    ctx.register_hook("post_llm_call", _wrap(_post_llm_call))
    ctx.register_hook("pre_tool_call", _wrap(_pre_tool_call))
    ctx.register_hook("post_tool_call", _wrap(_post_tool_call))

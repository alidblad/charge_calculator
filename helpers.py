"""Small shared helpers."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from homeassistant.util import dt as dt_util


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def coerce_value(value: Any) -> Any:
    """Turn a rendered placeholder string back into a number or bool where possible."""
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
        try:
            return int(stripped)
        except ValueError:
            return value

    try:
        return float(stripped)
    except ValueError:
        return value


def render_service_data(value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_service_data(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_service_data(item, context) for item in value]
    if isinstance(value, str):
        return coerce_value(value.format_map(_SafeFormatDict(context)))
    return value


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def ensure_dt(value: Any) -> Optional[datetime.datetime]:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return dt_util.utc_from_timestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    return dt_util.parse_datetime(str(value))


def to_timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = ensure_dt(value)
    return dt_util.as_timestamp(parsed) if parsed else None


def local_iso(value: Any) -> Optional[str]:
    parsed = ensure_dt(value)
    return dt_util.as_local(parsed).isoformat() if parsed else None


def local_hm(value: Any) -> str:
    """Short local time for log lines."""
    parsed = ensure_dt(value)
    return dt_util.as_local(parsed).strftime("%a %H:%M") if parsed else "-"


def parse_time_of_day(value: Any) -> Optional[datetime.time]:
    if isinstance(value, datetime.time):
        return value
    if value is None:
        return None
    parsed = dt_util.parse_time(str(value))
    return parsed


def next_occurrence(time_of_day: datetime.time, after: datetime.datetime) -> datetime.datetime:
    """Next local wall-clock occurrence of a time of day, as UTC."""
    local_after = dt_util.as_local(after)
    candidate = local_after.replace(
        hour=time_of_day.hour,
        minute=time_of_day.minute,
        second=time_of_day.second,
        microsecond=0,
    )
    if candidate <= local_after:
        candidate += datetime.timedelta(days=1)
    return dt_util.as_utc(candidate)


def config_get(cfg: Any, path: List[str], default: Any = None) -> Any:
    node = cfg
    for part in path:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def day_type(value: datetime.datetime) -> str:
    return "weekend" if dt_util.as_local(value).weekday() >= 5 else "weekday"

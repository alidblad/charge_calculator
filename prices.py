"""Normalised Nordpool price horizon."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from homeassistant.util import dt as dt_util

from .helpers import ensure_dt

_LOGGER = logging.getLogger(__name__)


class PriceHorizon:
    """All future price periods published by the Nordpool entity, normalised and sorted."""

    def __init__(self, nordpol_state: Any, time_now):
        self.attributes = getattr(nordpol_state, "attributes", {}) or {}
        self.time_now = time_now
        self.periods = self._load_periods()
        self.period_minutes = self._detect_period_minutes()

    def __bool__(self) -> bool:
        return bool(self.periods)

    @property
    def tomorrow_valid(self) -> bool:
        return bool(self.attributes.get("tomorrow_valid"))

    @property
    def period_hours(self) -> float:
        return self.period_minutes / 60.0

    @property
    def end(self):
        return self.periods[-1]["end"] if self.periods else None

    def _normalize(self, period: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        start = ensure_dt(period.get("start"))
        end = ensure_dt(period.get("end"))
        if start is None or end is None or end <= start:
            return None
        try:
            value = float(period.get("value"))
        except (TypeError, ValueError):
            return None
        return {"start": start, "end": end, "value": value}

    def _load_periods(self) -> List[Dict[str, Any]]:
        combined: List[Dict[str, Any]] = []
        for key in ("raw_today", "raw_tomorrow"):
            for raw in self.attributes.get(key, []) or []:
                normalised = self._normalize(raw)
                if normalised and normalised["end"] > self.time_now:
                    combined.append(normalised)
        combined.sort(key=lambda item: item["start"])
        return combined

    def _detect_period_minutes(self) -> int:
        for period in self.periods:
            minutes = int(round((period["end"] - period["start"]).total_seconds() / 60))
            if minutes > 0:
                return minutes
        return 60

    def periods_for_hours(self, hours: float) -> int:
        if hours <= 0:
            return 0
        return max(1, math.ceil((hours * 60) / self.period_minutes))

    def periods_for_minutes(self, minutes: Any) -> int:
        try:
            value = float(minutes)
        except (TypeError, ValueError):
            return 1
        if value <= 0:
            return 1
        return max(1, math.ceil(value / self.period_minutes))

    def before(self, deadline) -> List[Dict[str, Any]]:
        """Periods that finish no later than the deadline."""
        if deadline is None:
            return list(self.periods)
        return [period for period in self.periods if period["end"] <= deadline]

    def between(self, start, end) -> List[Dict[str, Any]]:
        return [
            period
            for period in self.periods
            if period["end"] > start and period["start"] < end
        ]

    def price_at(self, moment) -> Optional[float]:
        for period in self.periods:
            if period["start"] <= moment < period["end"]:
                return period["value"]
        return None

    def stats(self) -> Dict[str, Any]:
        if not self.periods:
            return {
                "count": 0,
                "min": None,
                "max": None,
                "avg": None,
                "min_at": None,
                "max_at": None,
                "horizon_end": None,
            }
        cheapest = min(self.periods, key=lambda p: p["value"])
        priciest = max(self.periods, key=lambda p: p["value"])
        return {
            "count": len(self.periods),
            "min": round(cheapest["value"], 4),
            "max": round(priciest["value"], 4),
            "avg": sum(p["value"] for p in self.periods) / len(self.periods),
            "min_at": cheapest["start"],
            "max_at": priciest["start"],
            "horizon_end": self.periods[-1]["end"],
        }

    def expected_discharge_price(self, period_count: int) -> Optional[float]:
        """Average of the priciest periods the stored energy could realistically replace."""
        if period_count <= 0 or not self.periods:
            return None
        top = sorted((p["value"] for p in self.periods), reverse=True)[:period_count]
        return sum(top) / len(top) if top else None

    def as_attribute_list(self) -> List[Dict[str, Any]]:
        return [
            {"start": dt_util.as_local(p["start"]).isoformat(), "value": round(p["value"], 4)}
            for p in self.periods
        ]

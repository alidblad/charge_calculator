"""Learns how fast the house battery drains, so the next charge can be planned around it."""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DAY_TYPES,
    DEFAULTS,
    DRAIN_STORAGE_KEY,
    DRAIN_STORAGE_VERSION,
)
from .helpers import as_float, day_type

_LOGGER = logging.getLogger(__name__)

MAX_SAMPLE_HOURS = 3.0
MIN_SAMPLE_MINUTES = 5.0


class DrainTracker:
    """Exponentially weighted drain profile, bucketed by day type and hour of day.

    A single flat average mis-predicts badly because evening load is far higher than
    night load, so each hour keeps its own estimate.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        soc_entity: Optional[str],
        battery_size: float,
        load_entity: Optional[str] = None,
        max_drain_kw: float = 50.0,
        alpha: float = DEFAULTS["drain_alpha"],
        min_samples: int = DEFAULTS["drain_min_samples"],
    ) -> None:
        self.hass = hass
        self.soc_entity = soc_entity
        self.load_entity = load_entity
        self.battery_size = battery_size
        self.max_drain_kw = max_drain_kw if max_drain_kw > 0 else 50.0
        self.alpha = alpha
        self.min_samples = min_samples
        self._store = Store(hass, DRAIN_STORAGE_VERSION, DRAIN_STORAGE_KEY)
        self._profile: Dict[str, List[Optional[float]]] = {
            key: [None] * 24 for key in DAY_TYPES
        }
        self._counts: Dict[str, List[int]] = {key: [0] * 24 for key in DAY_TYPES}
        self._last_sample: Optional[Dict[str, Any]] = None
        self._unsubscribe = None
        self._dirty = False

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        profile = stored.get("profile") or {}
        counts = stored.get("counts") or {}
        for key in DAY_TYPES:
            if isinstance(profile.get(key), list) and len(profile[key]) == 24:
                self._profile[key] = [as_float(v) for v in profile[key]]
            if isinstance(counts.get(key), list) and len(counts[key]) == 24:
                self._counts[key] = [int(v or 0) for v in counts[key]]
            for index, rate in enumerate(self._profile[key]):
                if rate is not None and (rate <= 0 or rate > self.max_drain_kw):
                    self._profile[key][index] = None
                    self._counts[key][index] = 0
                    self._dirty = True
        _LOGGER.debug("Loaded drain profile with %s learned hours", self.learned_hours)

    async def async_save(self) -> None:
        if not self._dirty:
            return
        await self._store.async_save({"profile": self._profile, "counts": self._counts})
        self._dirty = False

    @callback
    def async_start(self) -> None:
        tracked = [entity for entity in (self.load_entity, self.soc_entity) if entity]
        if not tracked:
            return
        self._unsubscribe = async_track_state_change_event(
            self.hass, tracked, self._handle_state_change
        )

    @callback
    def async_stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def learned_hours(self) -> int:
        return sum(
            1
            for key in DAY_TYPES
            for index, value in enumerate(self._profile[key])
            if value is not None and self._counts[key][index] >= self.min_samples
        )

    @property
    def has_data(self) -> bool:
        return self.learned_hours > 0

    @callback
    def _handle_state_change(self, event) -> None:
        if self.load_entity:
            self._sample_from_load()
        else:
            self._sample_from_soc()

    def _record(self, moment: datetime.datetime, kw: float) -> None:
        if kw <= 0 or kw > self.max_drain_kw:
            _LOGGER.debug(
                "Ignoring house drain sample %.3f kW outside the valid range (0, %.1f]",
                kw,
                self.max_drain_kw,
            )
            return
        bucket = day_type(moment)
        hour = dt_util.as_local(moment).hour
        previous = self._profile[bucket][hour]
        self._profile[bucket][hour] = (
            kw if previous is None else previous + self.alpha * (kw - previous)
        )
        self._counts[bucket][hour] += 1
        self._dirty = True

    def _sample_from_load(self) -> None:
        state = self.hass.states.get(self.load_entity)
        value = as_float(getattr(state, "state", None))
        if value is None:
            return
        unit = str(
            (getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")
        ).strip().lower()
        if unit in ("w", "watt", "watts"):
            kw = value / 1000.0
        elif unit in ("kw", "kilowatt", "kilowatts"):
            kw = value
        else:
            _LOGGER.debug(
                "Ignoring house load sample with unsupported unit %r", unit
            )
            return
        self._record(dt_util.utcnow(), kw)

    def _sample_from_soc(self) -> None:
        state = self.hass.states.get(self.soc_entity)
        soc = as_float(getattr(state, "state", None))
        if soc is None or self.battery_size <= 0:
            return

        now = dt_util.utcnow()
        previous = self._last_sample
        self._last_sample = {"soc": soc, "at": now}
        if previous is None:
            return

        hours = (now - previous["at"]).total_seconds() / 3600.0
        if hours <= MIN_SAMPLE_MINUTES / 60.0 or hours > MAX_SAMPLE_HOURS:
            return

        dropped_pct = previous["soc"] - soc
        if dropped_pct <= 0:
            return  # charging or idle, not a drain sample

        kw = (dropped_pct / 100.0) * self.battery_size / hours
        self._record(previous["at"], kw)

    def rate_at(self, moment: datetime.datetime) -> Optional[float]:
        bucket = day_type(moment)
        hour = dt_util.as_local(moment).hour
        if self._counts[bucket][hour] >= self.min_samples:
            return self._profile[bucket][hour]

        # Fall back to the average of whatever has been learned so far.
        known = [
            value
            for key in DAY_TYPES
            for index, value in enumerate(self._profile[key])
            if value is not None and self._counts[key][index] >= self.min_samples
        ]
        return sum(known) / len(known) if known else None

    def predict_kwh(self, start: datetime.datetime, end: datetime.datetime) -> Optional[float]:
        """Energy the house is expected to draw between two moments."""
        if end <= start or not self.has_data:
            return None
        total = 0.0
        cursor = start
        while cursor < end:
            next_hour = (cursor + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            slice_end = min(next_hour, end)
            rate = self.rate_at(cursor)
            if rate is None:
                return None
            total += rate * (slice_end - cursor).total_seconds() / 3600.0
            cursor = slice_end
        return total

    def hours_until(
        self,
        soc_pct: float,
        reserve_pct: float,
        start: datetime.datetime,
        limit_hours: float = 48.0,
    ) -> Optional[float]:
        """How long the battery lasts before hitting the reserve, walking the hourly profile."""
        if not self.has_data or self.battery_size <= 0:
            return None
        available = ((soc_pct - reserve_pct) / 100.0) * self.battery_size
        if available <= 0:
            return 0.0

        elapsed = 0.0
        cursor = start
        while elapsed < limit_hours:
            rate = self.rate_at(cursor)
            if rate is None or rate <= 0:
                return None
            step_end = (cursor + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            step_hours = max((step_end - cursor).total_seconds() / 3600.0, 1 / 60.0)
            step_energy = rate * step_hours
            if step_energy >= available:
                return elapsed + available / rate
            available -= step_energy
            elapsed += step_hours
            cursor = step_end
        return limit_hours

    def diagnostics(self) -> Dict[str, Any]:
        now = dt_util.utcnow()
        current = self.rate_at(now)
        return {
            "current_rate_kw": round(current, 3) if current is not None else None,
            "learned_hours": self.learned_hours,
            "source": "load sensor" if self.load_entity else "state of charge",
            "profile_weekday": [
                round(v, 3) if v is not None else None for v in self._profile["weekday"]
            ],
            "profile_weekend": [
                round(v, 3) if v is not None else None for v in self._profile["weekend"]
            ],
            "samples_weekday": list(self._counts["weekday"]),
            "samples_weekend": list(self._counts["weekend"]),
        }

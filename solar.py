"""Estimates PV production from an SMHI (or any) weather forecast.

SMHI does not publish irradiance, so production is approximated from solar elevation
and forecast cloud coverage. It is only accurate enough to answer "will solar refill
the battery tomorrow?", which is all the planner asks of it.
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DEFAULTS
from .helpers import as_float, ensure_dt

_LOGGER = logging.getLogger(__name__)

# Rough cloud transmission per HA weather condition, used when cloud_coverage is missing.
CONDITION_CLOUD = {
    "sunny": 0,
    "clear-night": 0,
    "windy": 10,
    "partlycloudy": 40,
    "windy-variant": 50,
    "cloudy": 90,
    "fog": 95,
    "rainy": 95,
    "snowy-rainy": 95,
    "pouring": 100,
    "lightning": 100,
    "lightning-rainy": 100,
    "snowy": 100,
    "hail": 100,
    "exceptional": 50,
}


class SolarForecast:
    """Expected PV energy per hour, derived from a weather forecast."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_entity: Optional[str],
        peak_kw: float,
        cloud_impact: float = DEFAULTS["solar_cloud_impact"],
    ) -> None:
        self.hass = hass
        self.weather_entity = weather_entity
        self.peak_kw = peak_kw
        self.cloud_impact = min(max(cloud_impact, 0.0), 1.0)
        self._forecast: List[Dict[str, Any]] = []
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self.peak_kw > 0

    async def async_refresh(self) -> None:
        self._forecast = []
        self._available = False
        if not self.weather_entity or self.peak_kw <= 0:
            return

        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly", "entity_id": self.weather_entity},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - never let the forecast break planning
            _LOGGER.warning("Could not fetch forecast from %s: %s", self.weather_entity, err)
            return

        entries = (response or {}).get(self.weather_entity, {}).get("forecast") or []
        for entry in entries:
            moment = ensure_dt(entry.get("datetime"))
            if moment is None:
                continue
            self._forecast.append(
                {
                    "start": moment,
                    "cloud": self._cloud_of(entry),
                    "condition": entry.get("condition"),
                }
            )
        self._forecast.sort(key=lambda item: item["start"])
        self._available = bool(self._forecast)
        _LOGGER.debug("Loaded %s hourly forecast entries from %s", len(self._forecast), self.weather_entity)

    def _cloud_of(self, entry: Dict[str, Any]) -> float:
        cloud = as_float(entry.get("cloud_coverage"))
        if cloud is None:
            cloud = CONDITION_CLOUD.get(entry.get("condition"), 50)
        return min(max(float(cloud), 0.0), 100.0)

    def _cloud_at(self, moment: datetime.datetime) -> float:
        chosen = 50.0
        for entry in self._forecast:
            if entry["start"] <= moment:
                chosen = entry["cloud"]
            else:
                break
        return chosen

    def _elevation_factor(self, moment: datetime.datetime) -> float:
        """sin(solar elevation), clamped at zero, as a stand-in for clear-sky irradiance."""
        try:
            from homeassistant.helpers.sun import get_astral_location

            location, elevation = get_astral_location(self.hass)
            angle = location.solar_elevation(moment, elevation)
        except Exception:  # noqa: BLE001 - astral is optional at runtime
            return self._daylight_fallback(moment)
        return max(math.sin(math.radians(angle)), 0.0)

    def _daylight_fallback(self, moment: datetime.datetime) -> float:
        hour = dt_util.as_local(moment).hour + dt_util.as_local(moment).minute / 60.0
        if hour <= 6 or hour >= 20:
            return 0.0
        return math.sin(math.pi * (hour - 6) / 14.0)

    def power_kw_at(self, moment: datetime.datetime) -> float:
        if not self.available:
            return 0.0
        elevation_factor = self._elevation_factor(moment)
        if elevation_factor <= 0:
            return 0.0
        cloud_factor = 1.0 - self.cloud_impact * (self._cloud_at(moment) / 100.0)
        return self.peak_kw * elevation_factor * cloud_factor

    def expected_kwh(self, start: datetime.datetime, end: datetime.datetime) -> float:
        """Integrate expected production in 30 minute steps."""
        if not self.available or end <= start:
            return 0.0
        total = 0.0
        step = datetime.timedelta(minutes=30)
        cursor = start
        while cursor < end:
            slice_end = min(cursor + step, end)
            hours = (slice_end - cursor).total_seconds() / 3600.0
            midpoint = cursor + (slice_end - cursor) / 2
            total += self.power_kw_at(midpoint) * hours
            cursor = slice_end
        return total

    def diagnostics(self, start: datetime.datetime) -> Dict[str, Any]:
        if not self.available:
            return {"available": False, "entity": self.weather_entity, "peak_kw": self.peak_kw}
        tomorrow_start = (dt_util.as_local(start) + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return {
            "available": True,
            "entity": self.weather_entity,
            "peak_kw": self.peak_kw,
            "rest_of_today_kwh": round(
                self.expected_kwh(start, dt_util.as_utc(tomorrow_start)), 2
            ),
            "tomorrow_kwh": round(
                self.expected_kwh(
                    dt_util.as_utc(tomorrow_start),
                    dt_util.as_utc(tomorrow_start + datetime.timedelta(days=1)),
                ),
                2,
            ),
            "cloud_now": self._cloud_at(start),
        }

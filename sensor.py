"""Sensors that expose the calculated charge plan in the Home Assistant UI."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LABELS, LABEL_NAMES, SIGNAL_PLAN_UPDATED

PLAN_ATTRIBUTE_KEYS = (
    "reason",
    "sessions",
    "session_count",
    "next_start",
    "next_stop",
    "current_pct",
    "target_pct",
    "charge_power_kw",
    "hours_needed",
    "periods_needed",
    "max_sessions",
    "average_price",
    "plan_cost",
    "baseline_cost",
    "saving_vs_single_window",
    "effective_charge_price",
    "expected_discharge_price",
    "minutes_remaining",
    "price_min",
    "price_max",
    "price_avg",
    "horizon_end",
    "tomorrow_prices_available",
    "updated_at",
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    if discovery_info is None:
        return

    entities: List[SensorEntity] = [ChargeCalculatorPriceSensor(hass)]
    for label in LABELS:
        entities.extend(
            [
                ChargeCalculatorPlanSensor(hass, label),
                ChargeCalculatorScheduleSensor(hass, label, "next_start", "next charge start"),
                ChargeCalculatorScheduleSensor(hass, label, "next_stop", "next charge stop"),
                ChargeCalculatorAveragePriceSensor(hass, label),
            ]
        )
    async_add_entities(entities)


class ChargeCalculatorSensor(SensorEntity):
    """Base sensor that redraws whenever a new plan is published."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, label: Optional[str], key: str, name: str) -> None:
        self.hass = hass
        self._label = label
        self._attr_unique_id = f"{DOMAIN}_{label}_{key}" if label else f"{DOMAIN}_{key}"
        self._attr_name = f"Charge calculator {name}"

    @property
    def _runtime(self) -> Dict[str, Any]:
        return self.hass.data.get(DOMAIN, {}) or {}

    @property
    def _plan(self) -> Dict[str, Any]:
        return (self._runtime.get("plans") or {}).get(self._label, {})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PLAN_UPDATED, self._handle_plan_update)
        )

    @callback
    def _handle_plan_update(self, label: Optional[str] = None) -> None:
        if self._label is None or label is None or label == self._label:
            self.async_write_ha_state()


class ChargeCalculatorPlanSensor(ChargeCalculatorSensor):
    """State is the plan status; the full plan sits in the attributes."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, hass: HomeAssistant, label: str) -> None:
        super().__init__(hass, label, "plan", f"{LABEL_NAMES[label]} plan")

    @property
    def native_value(self) -> Optional[str]:
        return self._plan.get("status")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = self._plan
        return {key: plan[key] for key in PLAN_ATTRIBUTE_KEYS if key in plan}


class ChargeCalculatorScheduleSensor(ChargeCalculatorSensor):
    """Timestamp of the next planned start or stop."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hass: HomeAssistant, label: str, key: str, name: str) -> None:
        super().__init__(hass, label, key, f"{LABEL_NAMES[label]} {name}")
        self._plan_key = key

    @property
    def native_value(self) -> Optional[datetime.datetime]:
        value = self._plan.get(self._plan_key)
        if not value:
            return None
        return dt_util.parse_datetime(value)


class ChargeCalculatorAveragePriceSensor(ChargeCalculatorSensor):
    """Average price of the planned charging windows."""

    _attr_icon = "mdi:cash-clock"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant, label: str) -> None:
        super().__init__(hass, label, "plan_price", f"{LABEL_NAMES[label]} plan price")

    @property
    def native_value(self) -> Optional[float]:
        value = self._plan.get("average_price")
        return round(float(value), 4) if value is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = self._plan
        return {
            "plan_cost": plan.get("plan_cost"),
            "baseline_cost": plan.get("baseline_cost"),
            "saving_vs_single_window": plan.get("saving_vs_single_window"),
            "price_avg": plan.get("price_avg"),
        }


class ChargeCalculatorPriceSensor(ChargeCalculatorSensor):
    """Current price, with the whole horizon and planned windows as attributes for charting."""

    _attr_icon = "mdi:chart-line"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, None, "current_price", "current price")

    def _prices(self) -> List[Dict[str, Any]]:
        return self._runtime.get("prices") or []

    @property
    def native_value(self) -> Optional[float]:
        now = dt_util.now()
        current = None
        for period in self._prices():
            start = dt_util.parse_datetime(period["start"])
            if start is not None and start <= now:
                current = period["value"]
            else:
                break
        return current

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        stats = self._runtime.get("price_stats") or {}
        plans = self._runtime.get("plans") or {}
        return {
            "prices": self._prices(),
            "windows": [
                {**session, "label": label}
                for label, plan in plans.items()
                for session in plan.get("sessions", [])
            ],
            "price_min": stats.get("min"),
            "price_max": stats.get("max"),
            "price_avg": round(stats["avg"], 4) if stats.get("avg") is not None else None,
        }

"""Sensors that expose the calculated plan, drain profile and solar forecast."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LABEL_NAMES, SIGNAL_PLAN_UPDATED, TASKS

PLAN_ATTRIBUTE_KEYS = (
    "reason",
    "sessions",
    "session_count",
    "next_start",
    "next_stop",
    "current_pct",
    "target_pct",
    "reserve_pct",
    "charge_power_kw",
    "discharge_power_kw",
    "hours_needed",
    "periods_needed",
    "energy_needed_kwh",
    "solar_offset_kwh",
    "hours_until_reserve",
    "empty_at",
    "ready_by",
    "deadline_relaxed",
    "average_price",
    "plan_cost",
    "baseline_cost",
    "saving_vs_single_window",
    "effective_charge_price",
    "expected_discharge_price",
    "discharge_price",
    "reference_charge_price",
    "spread",
    "min_spread",
    "available_kwh",
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

    entities: List[SensorEntity] = [
        ChargeCalculatorPriceSensor(hass),
        HouseDrainSensor(hass),
        HouseRunwaySensor(hass),
        SolarForecastSensor(hass),
    ]
    for task in TASKS:
        entities.append(PlanSensor(hass, task))
        entities.append(ScheduleSensor(hass, task, "next_start", "next start"))
        entities.append(ScheduleSensor(hass, task, "next_stop", "next stop"))
        entities.append(PlanPriceSensor(hass, task))
    async_add_entities(entities)


class BaseSensor(SensorEntity):
    """Redraws whenever the coordinator publishes a new plan."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, task: Optional[str], key: str, name: str) -> None:
        self.hass = hass
        self._task = task
        self._attr_unique_id = f"{DOMAIN}_{task}_{key}" if task else f"{DOMAIN}_{key}"
        self._attr_name = f"Charge calculator {name}"

    @property
    def _coordinator(self):
        return (self.hass.data.get(DOMAIN) or {}).get("coordinator")

    @property
    def _plan(self) -> Dict[str, Any]:
        coordinator = self._coordinator
        if coordinator is None:
            return {}
        return coordinator.plans.get(self._task, {})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PLAN_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self, task: Optional[str] = None) -> None:
        if self._task is None or task is None or task == self._task:
            self.async_write_ha_state()


class PlanSensor(BaseSensor):
    """State is the plan status; the full plan sits in the attributes."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, hass: HomeAssistant, task: str) -> None:
        super().__init__(hass, task, "plan", f"{LABEL_NAMES[task]} plan")

    @property
    def native_value(self) -> Optional[str]:
        return self._plan.get("status")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = self._plan
        return {key: plan[key] for key in PLAN_ATTRIBUTE_KEYS if key in plan}


class ScheduleSensor(BaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hass: HomeAssistant, task: str, key: str, name: str) -> None:
        super().__init__(hass, task, key, f"{LABEL_NAMES[task]} {name}")
        self._plan_key = key

    @property
    def native_value(self) -> Optional[datetime.datetime]:
        value = self._plan.get(self._plan_key)
        return dt_util.parse_datetime(value) if value else None


class PlanPriceSensor(BaseSensor):
    _attr_icon = "mdi:cash-clock"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant, task: str) -> None:
        super().__init__(hass, task, "plan_price", f"{LABEL_NAMES[task]} plan price")

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
        }


class ChargeCalculatorPriceSensor(BaseSensor):
    """Current price, with the whole horizon and planned windows for charting."""

    _attr_icon = "mdi:chart-line"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, None, "current_price", "current price")

    @property
    def native_value(self) -> Optional[float]:
        coordinator = self._coordinator
        if coordinator is None or coordinator.horizon is None:
            return None
        price = coordinator.horizon.price_at(dt_util.utcnow())
        return round(price, 4) if price is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        coordinator = self._coordinator
        if coordinator is None:
            return {}
        stats = coordinator.price_stats or {}
        return {
            "prices": coordinator.prices,
            "windows": [
                {**session, "task": task}
                for task, plan in coordinator.plans.items()
                for session in plan.get("sessions", [])
            ],
            "price_min": stats.get("min"),
            "price_max": stats.get("max"),
            "price_avg": round(stats["avg"], 4) if stats.get("avg") is not None else None,
        }


class HouseDrainSensor(BaseSensor):
    """Learned house consumption for the current hour."""

    _attr_icon = "mdi:home-lightning-bolt"
    _attr_native_unit_of_measurement = "kW"
    _attr_suggested_display_precision = 3

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, None, "house_drain", "house drain rate")

    @property
    def native_value(self) -> Optional[float]:
        coordinator = self._coordinator
        if coordinator is None:
            return None
        return coordinator.drain.diagnostics()["current_rate_kw"]

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        coordinator = self._coordinator
        return coordinator.drain.diagnostics() if coordinator else {}


class HouseRunwaySensor(BaseSensor):
    """Hours until the house battery reaches its reserve."""

    _attr_icon = "mdi:battery-clock"
    _attr_native_unit_of_measurement = "h"
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, "house", "runway", "house battery runway")

    @property
    def native_value(self) -> Optional[float]:
        return self._plan.get("hours_until_reserve")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = self._plan
        return {
            "empty_at": plan.get("empty_at"),
            "reserve_pct": plan.get("reserve_pct"),
            "current_pct": plan.get("current_pct"),
        }


class SolarForecastSensor(BaseSensor):
    """Expected PV production for the rest of today."""

    _attr_icon = "mdi:solar-power"
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, None, "solar_forecast", "solar forecast today")

    def _diagnostics(self) -> Dict[str, Any]:
        coordinator = self._coordinator
        if coordinator is None:
            return {}
        return coordinator.solar.diagnostics(dt_util.utcnow())

    @property
    def available(self) -> bool:
        return bool(self._diagnostics().get("available"))

    @property
    def native_value(self) -> Optional[float]:
        return self._diagnostics().get("rest_of_today_kwh")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return self._diagnostics()

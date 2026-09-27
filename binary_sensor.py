"""Binary sensors showing whether a battery is currently charging or discharging."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    DOMAIN,
    HOUSE_DISCHARGE,
    LABEL_NAMES,
    SIGNAL_PLAN_UPDATED,
    STATUS_CHARGING,
    STATUS_DISCHARGING,
    TASKS,
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    if discovery_info is None:
        return

    entities: List[BinarySensorEntity] = [ActiveSessionSensor(hass, task) for task in TASKS]
    async_add_entities(entities)


class ActiveSessionSensor(BinarySensorEntity):
    """On while the integration has an active, pinned session."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, task: str) -> None:
        self.hass = hass
        self._task = task
        self._active_status = STATUS_DISCHARGING if task == HOUSE_DISCHARGE else STATUS_CHARGING
        self._attr_unique_id = f"{DOMAIN}_{task}_active"
        self._attr_name = f"Charge calculator {LABEL_NAMES[task]} active"
        self._attr_device_class = (
            None if task == HOUSE_DISCHARGE else BinarySensorDeviceClass.BATTERY_CHARGING
        )

    @property
    def _plan(self) -> Dict[str, Any]:
        coordinator = (self.hass.data.get(DOMAIN) or {}).get("coordinator")
        return coordinator.plans.get(self._task, {}) if coordinator else {}

    @property
    def is_on(self) -> bool:
        return self._plan.get("status") == self._active_status

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = self._plan
        return {
            "start": plan.get("next_start"),
            "stop": plan.get("next_stop"),
            "minutes_remaining": plan.get("minutes_remaining"),
            "target_pct": plan.get("target_pct"),
            "charge_power_kw": plan.get("charge_power_kw"),
            "average_price": plan.get("average_price"),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PLAN_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self, task: Optional[str] = None) -> None:
        if task is None or task == self._task:
            self.async_write_ha_state()

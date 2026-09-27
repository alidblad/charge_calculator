"""Binary sensors showing whether a battery is currently charging."""
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

from .const import DOMAIN, LABELS, LABEL_NAMES, SIGNAL_PLAN_UPDATED, STATUS_CHARGING


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    if discovery_info is None:
        return

    entities: List[BinarySensorEntity] = [
        ChargeCalculatorChargingSensor(hass, label) for label in LABELS
    ]
    async_add_entities(entities)


class ChargeCalculatorChargingSensor(BinarySensorEntity):
    """On while the integration has an active, pinned charging session."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, hass: HomeAssistant, label: str) -> None:
        self.hass = hass
        self._label = label
        self._attr_unique_id = f"{DOMAIN}_{label}_charging"
        self._attr_name = f"Charge calculator {LABEL_NAMES[label]} charging"

    @property
    def _runtime(self) -> Dict[str, Any]:
        return self.hass.data.get(DOMAIN, {}) or {}

    @property
    def is_on(self) -> bool:
        plan = (self._runtime.get("plans") or {}).get(self._label, {})
        return plan.get("status") == STATUS_CHARGING

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan = (self._runtime.get("plans") or {}).get(self._label, {})
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
            async_dispatcher_connect(self.hass, SIGNAL_PLAN_UPDATED, self._handle_plan_update)
        )

    @callback
    def _handle_plan_update(self, label: Optional[str] = None) -> None:
        if label is None or label == self._label:
            self.async_write_ha_state()

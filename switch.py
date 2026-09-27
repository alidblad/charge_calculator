"""Switches to turn scheduling on and off per battery."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, LABEL_NAMES, SIGNAL_PLAN_UPDATED, TASKS


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    if discovery_info is None:
        return

    entities: List[SwitchEntity] = [SchedulingSwitch(hass, task) for task in TASKS]
    async_add_entities(entities)


class SchedulingSwitch(SwitchEntity):
    """Turning this off stops any running session and skips planning for that battery."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-check"

    def __init__(self, hass: HomeAssistant, task: str) -> None:
        self.hass = hass
        self._task = task
        self._attr_unique_id = f"{DOMAIN}_{task}_scheduling"
        self._attr_name = f"Charge calculator {LABEL_NAMES[task]} scheduling"

    @property
    def _coordinator(self):
        return (self.hass.data.get(DOMAIN) or {}).get("coordinator")

    @property
    def is_on(self) -> bool:
        coordinator = self._coordinator
        return bool(coordinator.enabled.get(self._task, True)) if coordinator else True

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        coordinator = self._coordinator
        if coordinator is None:
            return {}
        entity = coordinator.enable_entity(self._task)
        return {
            "external_enable_entity": entity,
            "effectively_enabled": coordinator.is_enabled(self._task),
            "plan_status": coordinator.plans.get(self._task, {}).get("status"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        coordinator = self._coordinator
        if coordinator is None:
            return
        await coordinator.async_set_enabled(self._task, value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PLAN_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self, task: Optional[str] = None) -> None:
        if task is None or task == self._task:
            self.async_write_ha_state()

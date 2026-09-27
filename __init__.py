"""Custom component Charge Calculator."""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import DEFAULTS, DOMAIN
from .coordinator import ChargeCoordinator
from .helpers import as_int

_LOGGER = logging.getLogger(__name__)

OVERRIDE_KEYS = (
    "car_charge_effect",
    "car_charge_stop",
    "car_max_sessions",
    "house_charge_effect",
    "house_charge_stop",
    "house_max_sessions",
)

PLATFORMS = ("sensor", "binary_sensor")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the charge_calculator component."""
    cfg = config.get(DOMAIN, {}) or {}
    coordinator = ChargeCoordinator(hass, cfg)
    await coordinator.async_load()

    runtime = hass.data.setdefault(DOMAIN, {})
    runtime["coordinator"] = coordinator

    if not cfg:
        _LOGGER.warning(
            "No configuration found for domain '%s'. The service is still available.", DOMAIN
        )

    async def calculate_charge(call: ServiceCall) -> Dict[str, Any]:
        overrides = {key: call.data.get(key) for key in OVERRIDE_KEYS if key in call.data}
        return await coordinator.async_run(
            overrides=overrides,
            execute=bool(call.data.get("execute_actions", False)),
        )

    hass.services.async_register(
        DOMAIN,
        "calculate_charge",
        calculate_charge,
        supports_response=SupportsResponse.OPTIONAL,
    )

    if not cfg:
        return True

    interval_minutes = as_int(cfg.get("interval_minutes"), DEFAULTS["interval_minutes"])
    if not interval_minutes or interval_minutes <= 0:
        interval_minutes = DEFAULTS["interval_minutes"]
        _LOGGER.warning("Invalid interval_minutes, using %s", interval_minutes)

    throttle = datetime.timedelta(
        seconds=as_int(
            cfg.get("event_throttle_seconds"), DEFAULTS["event_throttle_seconds"]
        )
    )

    @callback
    def on_interval(_: datetime.datetime) -> None:
        hass.async_create_task(coordinator.async_run(execute=True))

    @callback
    def on_price_update(_event) -> None:
        now = dt_util.utcnow()
        last_run: Optional[datetime.datetime] = runtime.get("last_event_run")
        if last_run is not None and now - last_run < throttle:
            return
        runtime["last_event_run"] = now
        hass.async_create_task(coordinator.async_run(execute=True))

    runtime["periodic_unsubscribe"] = async_track_time_interval(
        hass, on_interval, datetime.timedelta(minutes=interval_minutes)
    )

    # Nordpool publishes tomorrow's prices around 13:00; react to that rather than waiting.
    nordpol_entity = cfg.get("nordpol_entity")
    if nordpol_entity:
        runtime["price_unsubscribe"] = async_track_state_change_event(
            hass, [nordpol_entity], on_price_update
        )

    coordinator.drain.async_start()

    for platform in PLATFORMS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))

    hass.async_create_task(coordinator.async_run(execute=True))

    return True

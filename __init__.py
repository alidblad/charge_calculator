"""Example of a custom component exposing a service."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.typing import ConfigType

# The domain of your component. Should be equal to the name of your component.
DOMAIN = "charge_calculator"
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the an async service example component."""
    @callback
    def calculate_charge_time(call: ServiceCall) -> None:
        """My first service."""
        _LOGGER.info(f"Received data, {call.data}")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

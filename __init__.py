"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.typing import ConfigType
from .const import DOMAIN 

DOMAIN = "charge_calculator"
_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the an async service charge_calculator."""
    @callback
    def calculate_charge_time(call: ServiceCall) -> None:
        """Calculate when to charge."""
        _LOGGER.info(f"Received data, data={call.data}")
        _LOGGER.info(f"Nordpol entity={config[DOMAIN]['nordpol_entity']}")
        _LOGGER.info(f"Wether entity={config[DOMAIN]['wether_entity']}")
        nordpol_state = hass.states.get(config[DOMAIN]['nordpol_entity'])
        _LOGGER.info(f"state nordpol name={nordpol_state}")
        name = nordpol_state.name
        _LOGGER.info(f"state nordpol name={name}")
        _LOGGER.info(f"atributes nordpol={nordpol_state.as_dict}")


    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

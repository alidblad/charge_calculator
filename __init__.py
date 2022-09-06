
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import logging
from .const import *

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config_entry: ConfigEntry):
    
    def calculate_chage(service_call: ServiceCall):
        """Calculate chage."""
        result_state = service_call.data.get(ATTR_NAME, "default")
        _LOGGER.info(f"result_state = {result_state}.")
        hass.states.async_set(f"{DOMAIN}.result_state", result_state)


    _LOGGER.info(f"Register {DOMAIN} service.")
    _LOGGER.info(f"Config_etry: {config_entry}.")
    hass.services.async_register(DOMAIN, INTEGRATION_NAME, calculate_chage)
    return True
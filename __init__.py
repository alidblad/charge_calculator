"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant, ServiceCall, callback, State
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
        name = nordpol_state.name
        np_atattributes = nordpol_state.attributes
        raw_today = np_atattributes['raw_today']
        raw_tomorrow = np_atattributes['raw_tomorrow']
        lowest_price_today = get_lowest_price(raw_today)
        lowest_price_tomorrow = get_lowest_price(raw_tomorrow)
        _LOGGER.info(f"lowest_price_today={lowest_price_today}.")
        _LOGGER.info(f"lowest_price_tomorrow={lowest_price_tomorrow}.")

    def get_lowest_price(hour_pirces: list):
        _LOGGER.info(f"get_lowest_price hour_pirces={hour_pirces}.")
        lowest_price = 1000
        fail = False
        for price in hour_pirces:
            if price.value < lowest_price:
                lowest_price = price.value
        if lowest_price == 1000:
            _LOGGER.error(f"Error while calulate lowest price, hour_pirces={hour_pirces}."
        return lowest_price

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

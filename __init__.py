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
        _LOGGER.info(f"Nordpol name={name}")
        ch = ChargeCalculator(_LOGGER, nordpol_state)
        lowest_price_today = ch.get_lowest_price()
        _LOGGER.info(f"lowest_price_today={lowest_price_today}.")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

class ChargeCalculator:
    def __init__(self, logger: logging.Logger, nordpol_state):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = nordpol_state.attributes

    def get_lowest_price(self):
        raw_today = self.nordpol_attributes['raw_today']
        self.logger.info(f"get_lowest_price raw_today={raw_today}.")
        lowest_price = 1000
        
        for price in raw_today:
            self.logger.info(f"get_lowest_price price={price}.")
            if price['value'] < lowest_price:
                lowest_price = price['value']

        if lowest_price == 1000:
            self.logger.error(f"Error while calulate lowest price, hour_pirces={raw_today}.")
        self.logger.info(f"Lowest price, lowest_price={lowest_price}.")
        return lowest_price    
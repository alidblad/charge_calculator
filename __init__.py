"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
from time import time_ns
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
        time_now = hass.util.dt.utcnow()
        _LOGGER.info(f"Nordpol name={name}")
        ch = ChargeCalculator(_LOGGER, nordpol_state, time_now)
        aapp = ch.get_all_availible_price_periods()
        lowest_price_today = ch.get_lowest_price(aapp)
        _LOGGER.info(f"lowest_price_today={lowest_price_today}.")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

class ChargeCalculator:
    def __init__(self, logger: logging.Logger, nordpol_state, time_now):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = nordpol_state.attributes
        self.time_now = time_now

    def filter_future_prices(self, prices):
        fp = []
        for price in prices:
            if price['end'] > self.time_now:
                fp.append(price)
        return fp

    def get_all_availible_price_periods(self):
        aapp = []
        if "raw_today" in self.nordpol_attributes.keys():
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_today']))
        if "raw_tomorrow" in self.nordpol_attributes.keys():
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_tomorrow']))
        return aapp

    def get_lowest_price(self, aapp):
        self.logger.info(f"get_lowest_price aapp={aapp}.")
        lowest_price = 1000
        
        for price in aapp:
            self.logger.info(f"get_lowest_price price={price}.")
            if price['value'] < lowest_price:
                lowest_price = price['value']

        if lowest_price == 1000:
            self.logger.error(f"Error while calulate lowest price, hour_pirces={raw_today}.")
        self.logger.info(f"Lowest price, lowest_price={lowest_price}.")
        return lowest_price    
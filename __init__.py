"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.util import dt as dt_util
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
        time_now = dt_util.utcnow()
        _LOGGER.info(f"Nordpol name={name}")
        ch = ChargeCalculator(_LOGGER, nordpol_state, time_now)
        get_best_time_to_charge = ch.get_best_time_to_charge()
        _LOGGER.info(f"get_best_time_to_charge={get_best_time_to_charge}.")

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
        self.aapp = []

    def filter_future_prices(self, prices):
        fp = []
        for price in prices:
            if price['end'] > self.time_now:
                fp.append(price)
            else:
                self.logger.info(f"filter_future_prices price is in the past: {price}.")
        return fp

    def get_all_availible_price_periods(self):
        aapp = []
        if "raw_today" in self.nordpol_attributes.keys():
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_today']))
        if "raw_tomorrow" in self.nordpol_attributes.keys():
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_tomorrow']))
        return aapp

    def get_min_price_period(self, aapp):
        lowest_price_period = None
        for price in aapp:
            self.logger.info(f"get_min_price price={price}.")
            if lowest_price_period == None or price['value'] < lowest_price_period['value']:
                lowest_price_period = price
        return lowest_price_period

    def get_next_following_price(self, aapp, price_period, next_after=True):
        if next_after:
            start = "start"
            end = "end"
        else:
            start = "end"
            end = "start"
        self.logger.info(f"get_next_following_price start={start}, end={end}.")
        for price in aapp:
            if price_period['end'] == price[start]:
                self.logger.info(f"get_next_following_price = {price}.")
                return price
        self.logger.info(f"get_next_following_price not found...")                
        return None

    def price_diviation(self, mean_price):
        if mean_price < 1:
            return (mean_price / 2) + mean_price
        else:
            return (mean_price / 4) + mean_price

    def get_lowest_price_period(self, aapp):
        lowest_price = self.get_min_price_period(aapp)
        mean_price = lowest_price['value']
        price_period = lowest_price
        lowest_price_period = [ ]

        # Get all next following price within price_diviation
        while price_period['value'] < self.price_diviation(mean_price):
            next_following_price = self.get_next_following_price(aapp, price_period)
            if next_following_price != None:
                mean_price = (price_period['value'] + next_following_price['value']) / 2
                lowest_price_period.append(price_period)
                self.logger.info(f"get_lowest_price_period next_following_price={next_following_price}.") 
                price_period = next_following_price
            else:
                break
        
        self.logger.info(f"get_lowest_price_period lowest_price_period={lowest_price_period}.") 
        return lowest_price_period
    
    def get_best_time_to_charge(self):
        # Get all all availible price periods (aapp)
        aapp = self.get_all_availible_price_periods()
        # Sort aapp by ['end'] timestam
        # aapp.sort(key=lambda x: x['end'], reverse=False)
        self.logger.info(f"get_lowest_price aapp={aapp}.")
        
        lowest_price_period = self.get_lowest_price_period(aapp)        
        self.print_price_periods(lowest_price_period)
        self.logger.info(f"get_best_time_to_charge, {lowest_price_period[0]['start']} - {lowest_price_period[-1]['end']}")
        return "hej"    

    def print_price_periods(self, price_periods):
        for price_period in price_periods:
            self.logger.info(f"DEBUG: Start={price_period['start'].strftime('%Y-%m-%d %H:%M')}, End={price_period['end'].strftime('%Y-%m-%d %H:%M')}, Value={price_period['value']}.")

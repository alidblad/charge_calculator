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

        if "cutoff" in call.data:
            cutoff = call.data['cutoff']
        else:
            cutoff = 0.5

        if "charge_period" in call.data:
            charge_period = call.data['charge_period']
        else:
            charge_period = 3

        ch = ChargeCalculator(_LOGGER, nordpol_state, time_now, cutoff, charge_period)
        best_time_to_charge = ch.get_best_time_to_charge()
        _LOGGER.info(f"get_best_time_to_charge={best_time_to_charge}.")
        hass.states.set(f"{DOMAIN}.start_time", best_time_to_charge['start'])
        hass.states.set(f"{DOMAIN}.stop_time", best_time_to_charge['stop'])
        _LOGGER.info(f"Start and stop time set to ha state: {best_time_to_charge}.")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

class ChargeCalculator:
    def __init__(self, logger: logging.Logger, nordpol_state, time_now, price_cutoff, charge_period):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = nordpol_state.attributes
        self.time_now = time_now
        self.price_cutoff = price_cutoff
        self.charge_period = charge_period
        self.aapp = self.get_all_availible_price_periods()
        self.sd = self.standard_deviation(self.aapp)
        self.mean = self.calc_mean(self.aapp)
        self.logger.info(f"Time_now = {self.time_now}.")
        self.logger.info(f"price_cutoff = {self.price_cutoff}.")
        self.logger.info(f"charge_period = {self.charge_period}.")
        self.logger.info(f"sd = {self.sd}.")
        self.logger.info(f"mean = {self.mean}.")

    def filter_future_prices(self, prices):
        fp = []
        for price in prices:
            if price['end'] > self.time_now:
                fp.append(price)
            else:
                self.logger.debug(f"filter_future_prices price is in the past: {price}.")
        return fp

    def isfloat(self, num):
        if num is not None:
            try:
                float(num)
                return True
            except ValueError:
                return False
        return False

    def validade_price(self, price_periods):
        valid_values = True
        for price in price_periods:
            if not self.isfloat(price['value']):
                valid_values = False
                break
        return valid_values

    def get_all_availible_price_periods(self):
        aapp = []
        if "raw_today" in self.nordpol_attributes.keys() and self.validade_price(self.nordpol_attributes['raw_today']):
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_today']))
        if "raw_tomorrow" in self.nordpol_attributes.keys() and self.validade_price(self.nordpol_attributes['raw_tomorrow']):
            aapp.extend(self.filter_future_prices(self.nordpol_attributes['raw_tomorrow']))
        # Sort by end date
        aapp.sort(key=lambda x: x['end'], reverse=False)
        return aapp

    def get_min_price_period(self, aapp):
        lowest_price_period = None
        for price in aapp:
            if lowest_price_period == None or price['value'] < lowest_price_period['value']:
                lowest_price_period = price

        self.logger.info(f"lowest_price_period: {lowest_price_period}.")
        return lowest_price_period

    def get_next_following_price(self, aapp, price_period, next_after=True):
        if next_after:
            start = "start"
            end = "end"
        else:
            start = "end"
            end = "start"
        #self.logger.info(f"get_next_following_price start={start}, end={end}.")
        for price in aapp:
            if price_period[end] == price[start]:
                #self.logger.info(f"get_next_following_price = {price}.")
                return price
        self.logger.info(f"get_next_following_price not found...")                
        return None

    def get_next_following_price_periods(self, lowest_price, aapp, next_after=True):
        price_period = self.get_next_following_price(aapp, lowest_price, next_after)
        lowest_price_period = []
        low_price_cutoff = lowest_price['value'] + (self.sd * self.price_cutoff)
        # Get all next following price within standard_deviation
        while price_period['value'] < low_price_cutoff and price_period['value'] < self.mean:
            next_following_price = self.get_next_following_price(aapp, price_period, next_after)
            if next_following_price != None:
                lowest_price_period.append(price_period)
                #self.logger.info(f"get_lowest_price_period next_following_price={next_following_price}.") 
                price_period = next_following_price
            else:
                break
        return lowest_price_period

    def get_lowest_price_period(self, aapp, charge_period=0):
        lowest_price = self.get_min_price_period(aapp)
        # Get lowest price period from lowest and forward
        lowest_price_period = self.get_next_following_price_periods(lowest_price, aapp, True)
        # Get lowest price period from lowest and backward
        lowest_price_period.extend(self.get_next_following_price_periods(lowest_price, aapp, False))
        # Add lowest_price
        lowest_price_period.append(lowest_price)
        # Sort 
        lowest_price_period.sort(key=lambda x: x['end'], reverse=False)
        
        # filter charge_period
        if charge_period != 0:
            num_remove = len(lowest_price_period) - charge_period
            for i in range(1, num_remove):
                if (i % 2) == 0:
                   del lowest_price_period[0] 
                else:
                   del lowest_price_period[-1] 
        return lowest_price_period

    def standard_deviation(self, aapp):
        values = []
        for tp in aapp:
            values.append(tp['value'])

        self.logger.info(f"standard_deviation values={values}.") 
        #calculate population standard deviation of list 
        return (sum((x-(sum(values) / len(values)))**2 for x in values) / len(values))**0.5

    def calc_mean(self, aapp):
        sum_values = 0
        num_values = 0
        for pp in aapp:
            sum_values += pp['value']
            num_values += 1
        return sum_values / num_values

    def print_price_periods(self, price_periods):
        for price_period in price_periods:
            self.logger.info(f"DEBUG: Start={price_period['start'].strftime('%Y-%m-%d %H:%M')}, End={price_period['end'].strftime('%Y-%m-%d %H:%M')}, Value={price_period['value']}.")

    def get_best_time_to_charge(self):    
        lowest_price_period = self.get_lowest_price_period(self.aapp, self.charge_period)        
        self.print_price_periods(lowest_price_period)
        self.logger.info(f"get_best_time_to_charge, {lowest_price_period[0]['start']} - {lowest_price_period[-1]['end']}")
        return { "start": lowest_price_period[0]['start'], "stop": lowest_price_period[-1]['end'] }   

"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
import datetime
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

        input_datetime_start = config[DOMAIN]['input_datetime_start']
        input_datetime_stop = config[DOMAIN]['input_datetime_stop']
        _LOGGER.info(f"Start charge trigger input_datetine={input_datetime_start}")
        _LOGGER.info(f"Stop charge trigger input_datetine={input_datetime_stop}")

        nordpol_state = hass.states.get(config[DOMAIN]['nordpol_entity'])
        name = nordpol_state.name
        time_now = dt_util.utcnow()
        _LOGGER.info(f"Nordpol name={name}")

        if "charge_period" in call.data:
            charge_period = call.data['charge_period']
        else:
            charge_period = 3

        ch = ChargeCalculator(_LOGGER, nordpol_state, time_now, charge_period)
        best_time_to_charge = ch.get_best_time_to_charge()
        _LOGGER.info(f"get_best_time_to_charge={best_time_to_charge}.")
        hass.states.async_set(f"{DOMAIN}.start_time", best_time_to_charge['start'])
        hass.states.async_set(f"{DOMAIN}.stop_time", best_time_to_charge['stop'])
        _LOGGER.info(f"Start and stop time set to ha state: {best_time_to_charge}.")

        ts_start = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge['start'])))
        ts_stop = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge['stop'])))

        # Set component state
        hass.states.async_set(f"{DOMAIN}.start_time", ts_start)
        hass.states.async_set(f"{DOMAIN}.stop_time", ts_stop)
        _LOGGER.info(f"Entity '{DOMAIN}.start_time' has been updated: timestamp={ts_start}.")
        _LOGGER.info(f"Entity '{DOMAIN}.stop_time' has been updated: timestamp={ts_stop}.")

        #r1 = hass.services.async_call(
        #    "input_datetime",
        #    "set_datetime",
        #    {"data": { "timestamp": ts_start}, "target": {"entity_id": input_datetime_start }}
        #)   
        #_LOGGER.info(f"Call service input_datetime {input_datetime_start} timestamp={ts_start}: {r1}.")
        
        #r2 = hass.services.async_call(
        #    "input_datetime",
        #    "set_datetime",
        #    {"data": { "timestamp": ts_stop}, "target": {"entity_id": input_datetime_stop }}
        #)   
        #_LOGGER.info(f"Call service input_datetime {input_datetime_stop} timestamp={ts_stop}: {r2}")

        #input_datetime_start_state = hass.states.get(input_datetime_start)
        #input_datetime_stop_state = hass.states.get(input_datetime_stop)

        #_LOGGER.info(f"State of {input_datetime_start} = {input_datetime_start_state}.")
        #_LOGGER.info(f"State of {input_datetime_stop} = {input_datetime_stop_state}.")


    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    # Return boolean to indicate that initialization was successfully.
    return True

class ChargeCalculator:
    def __init__(self, logger: logging.Logger, nordpol_state, time_now, charge_period):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = nordpol_state.attributes
        self.time_now = time_now
        self.charge_period = charge_period
        self.aapp = self.next_day_pp_filter(self.get_all_availible_price_periods())
        self.logger.info(f"Time_now = {self.time_now}.")
        self.logger.info(f"charge_period = {self.charge_period}.")

    def filter_past_prices(self, prices):
        fp = []
        for price in prices:
            if price['end'] > self.time_now:
                fp.append(price)
            else:
                self.logger.debug(f"filter_future_prices price is in the past: {price}.")
        return fp

    def next_day_pp_filter(self, prices, hour=11, minute=00, second=00):
        fp = []
        # Tiem now date + 1 day + hour:minute:second
        cutoff = self.time_now + datetime.timedelta(days=1)
        self.logger.info(f"CUTOFF = {cutoff}.")

        for price in prices:
            if price['end'] < cutoff:
                fp.append(price)
            else:
                self.logger.debug(f"filter_prices_after price is older than cutoff: {price}.")
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
            aapp.extend(self.filter_past_prices(self.nordpol_attributes['raw_today']))
        if "raw_tomorrow" in self.nordpol_attributes.keys() and self.validade_price(self.nordpol_attributes['raw_tomorrow']):
            aapp.extend(self.filter_past_prices(self.nordpol_attributes['raw_tomorrow']))
        # Sort by end date
        aapp.sort(key=lambda x: x['end'], reverse=False)
        return aapp

    def calc_average_charge_price(self, aapp, charge_period):
        average_charge_prices = []
        for i in range(len(aapp)):
            sum_price = 0
            periods = []
            if i + charge_period <= len(aapp):
                for cp in range(charge_period):
                    index = int(i) + int(cp)
                    sum_price += aapp[index]['value']
                    periods.append(aapp[index])
            else:
                break
            average_charge_prices.append({ 'value': sum_price/charge_period, 'periods': periods })
        return average_charge_prices

    def get_lowest_average_charge_period(self, aapp, charge_period):
        average_charge_prices = self.calc_average_charge_price(aapp, charge_period)
        # Sort by end value
        average_charge_prices.sort(key=lambda x: x['value'], reverse=False)
        self.print_average_charge_periods(average_charge_prices)
        
        self.logger.info(f"Best charge period: {average_charge_prices[0]}.")
        return average_charge_prices[0]

    def print_price_periods(self, price_periods):
        self.logger.info(f"Print_price_periods:") 
        for price_period in price_periods:
            self.logger.info(f"DEBUG: Start={price_period['start'].strftime('%Y-%m-%d %H:%M')}, End={price_period['end'].strftime('%Y-%m-%d %H:%M')}, Value={price_period['value']}.")

    def print_average_charge_periods(self, average_charge_periods):
        self.logger.info(f"Print_average_charge_periods:") 
        for period in average_charge_periods:
            self.logger.info(f"DEBUG: Start={period['periods'][0]['start'].strftime('%Y-%m-%d %H:%M')}, End={period['periods'][-1]['end'].strftime('%Y-%m-%d %H:%M')}, Value={period['value']}.")

    def get_best_time_to_charge(self):
        best_charge_period = self.get_lowest_average_charge_period(self.aapp, self.charge_period)
        #lowest_price_period = self.get_lowest_price_period(self.aapp, self.charge_period)        
        self.print_price_periods(best_charge_period['periods'])
        self.logger.info(f"get_best_time_to_charge, {best_charge_period['periods'][0]['start']} - {best_charge_period['periods'][-1]['end']}")
        return { "start": best_charge_period['periods'][0]['start'], "stop": best_charge_period['periods'][-1]['end'] }   

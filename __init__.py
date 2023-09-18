"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
import datetime
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.typing import ConfigType
from .const import DOMAIN
import math

DOMAIN = "charge_calculator"
_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the an async service charge_calculator."""
    @callback
    def calculate_charge_time(call: ServiceCall) -> None:
        """Calculate when to charge."""
        _LOGGER.info(f"Charge-calculater START!")
        _LOGGER.info(f"Received data, data={call.data}")
        _LOGGER.info(f"Nordpol entity={config[DOMAIN]['nordpol_entity']}")
        _LOGGER.info(f"Wether entity={config[DOMAIN]['wether_entity']}")
        _LOGGER.info(f"Car battery entity={config[DOMAIN]['car_battery']['sensor_id']}")
        _LOGGER.info(f"House battery entity={config[DOMAIN]['house_battery']['sensor_id']}")

        # Get state of car sensor
        car_batterys_state = hass.states.get(config[DOMAIN]['car_battery']['sensor_id'])
        if car_batterys_state is None:
            _LOGGER.error(f"Could not get state of sensor: {config[DOMAIN]['car_battery']['sensor_id']}")

        # Get state of house sensor
        house_battery_state = hass.states.get(config[DOMAIN]['house_battery']['sensor_id'])
        if house_battery_state is None:
            _LOGGER.error(f"Could not get state of sensor: {config[DOMAIN]['house_battery']['sensor_id']}")

        # Get state of nordpol sensor
        nordpol_state = hass.states.get(config[DOMAIN]['nordpol_entity'])
        name = nordpol_state.name

        _LOGGER.info(f"car_batterys_state={car_batterys_state}")
        _LOGGER.info(f"house_battery_state={house_battery_state}")
        # Get time
        time_now = dt_util.utcnow()
        _LOGGER.info(f"Nordpol name={name}")

        # Get charge effect
        if "car_charge_effect" in call.data:
            car_charge_effect = float(call.data['car_charge_effect'])
        else:
            car_charge_effect = 6.6

        if "house_charge_effect" in call.data:
            house_charge_effect = float(call.data['house_charge_effect'])
        else:
            house_charge_effect = 4

        # Get charge stop car (default 80%)
        if "car_charge_stop" in call.data:
            car_charge_stop = int(call.data['car_charge_stop'])
        else:
            car_charge_stop = 80

        # Get charge stop house (default 90%)
        if "house_charge_stop" in call.data:
            house_charge_stop = int(call.data['house_charge_stop'])
        else:
            house_charge_stop = 90

        # Calculate charge time
        car_battery_size = int(config[DOMAIN]['car_battery']['size'])
        car_battery_effect = (float(car_batterys_state.state) / 100) * int(car_battery_size)
        car_stop_charge_at = (car_charge_stop / 100) * int(car_battery_size)
        charge_time_car = (car_stop_charge_at - car_battery_effect) / car_charge_effect
        charge_time_car_round = math.ceil(charge_time_car)
        _LOGGER.info(f"Calculated charge time for car: {charge_time_car}, round = {charge_time_car_round}.")
        if float(config[DOMAIN]['car_battery']['min_charge_time']) > charge_time_car_round:
            charge_time_car_round = int(config[DOMAIN]['car_battery']['min_charge_time'])
            _LOGGER.info(f"Charge time for car is less than min_charge_time, updated: {charge_time_car_round}.")

        house_battery_size = int(config[DOMAIN]['house_battery']['size'])
        house_battery_effect = (float(house_battery_state.state) / 100) * house_battery_size
        house_stop_charge_at = (house_charge_stop / 100) * house_battery_size
        charge_time_house = (house_stop_charge_at - house_battery_effect) / house_charge_effect
        charge_time_house_round = math.ceil(charge_time_house)
        _LOGGER.info(f"Calculated charge time for house: {charge_time_house}, round = {charge_time_house_round}.")
        if float(config[DOMAIN]['house_battery']['min_charge_time']) > charge_time_house_round:
            charge_time_house_round = int(config[DOMAIN]['house_battery']['min_charge_time'])
            _LOGGER.info(f"Charge time for house is less than min_charge_time, updated: {charge_time_car_round}.")

        # Claculate bets time to charge car
        if charge_time_car_round > 0:
            car_ch = ChargeCalculator(_LOGGER, nordpol_state, time_now, charge_time_car_round)
            best_time_to_charge_car = car_ch.get_best_time_to_charge()
            _LOGGER.info(f"get_best_time_to_charge_car={best_time_to_charge_car}.")
            _LOGGER.info(f"Start and stop time set to ha state: {best_time_to_charge_car}.")
            ts_start_car = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge_car['start'])))
            ts_stop_car = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge_car['stop'])))

            # Set car component state
            hass.states.async_set(f"{DOMAIN}.car_start_time", ts_start_car)
            hass.states.async_set(f"{DOMAIN}.car_stop_time", ts_stop_car)
            _LOGGER.info(f"Entity '{DOMAIN}.car_start_time' has been updated: timestamp={ts_start_car}.")
            _LOGGER.info(f"Entity '{DOMAIN}.car_stop_time' has been updated: timestamp={ts_stop_car}.")
        else:
            _LOGGER.info(f"charge_time_car_round is 0.")

        # Claculate bets time to charge house
        if charge_time_house_round > 0:        
            ch = ChargeCalculator(_LOGGER, nordpol_state, time_now, charge_time_house_round)
            best_time_to_charge_house = ch.get_best_time_to_charge()
            _LOGGER.info(f"best_time_to_charge_house={best_time_to_charge_house}.")
            _LOGGER.info(f"Start and stop time set to ha state: {best_time_to_charge_house}.")
            ts_start_house = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge_house['start'])))
            ts_stop_house = datetime.datetime.timestamp(datetime.datetime.fromisoformat(str(best_time_to_charge_house['stop'])))

            # Set component state
            hass.states.async_set(f"{DOMAIN}.house_start_time", ts_start_house)
            hass.states.async_set(f"{DOMAIN}.house_stop_time", ts_stop_house)
            _LOGGER.info(f"Entity '{DOMAIN}.house_start_time' has been updated: timestamp={ts_start_house}.")
            _LOGGER.info(f"Entity '{DOMAIN}.house_stop_time' has been updated: timestamp={ts_stop_house}.")
        else:
            _LOGGER.info(f"charge_time_house_round is 0.")

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
            _LOGGER.info(f"sum_price/charge_period: {sum_price}/{charge_period}.")
            average_charge_prices.append({ 'value': sum_price/charge_period, 'periods': periods })
        return average_charge_prices

    def get_lowest_average_charge_period(self, aapp, charge_period):
        average_charge_prices = self.calc_average_charge_price(aapp, charge_period)
        # Sort by end value
        average_charge_prices.sort(key=lambda x: x['value'], reverse=False)
        self.print_average_charge_periods(average_charge_prices)
        
        if len(average_charge_prices) > 0:
            self.logger.info(f"Best charge period: {average_charge_prices[0]}.")
            return average_charge_prices[0]
        else:
            return None

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
        if best_charge_period != None:
            self.print_price_periods(best_charge_period['periods'])
            self.logger.info(f"get_best_time_to_charge, {best_charge_period['periods'][0]['start']} - {best_charge_period['periods'][-1]['end']}")
            return { "start": best_charge_period['periods'][0]['start'], "stop": best_charge_period['periods'][-1]['end'] }   
        else:
            return {}

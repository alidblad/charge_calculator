"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
import datetime
import math
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "car_charge_effect": 6.6,
    "house_charge_effect": 4.0,
    "car_charge_stop": 80,
    "house_charge_stop": 90,
}

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the async service charge_calculator."""
    cfg = config.get(DOMAIN, {})
    if not cfg:
        _LOGGER.warning("No configuration found for domain '%s'. Service will still be available.", DOMAIN)

    @callback
    def calculate_charge_time(call: ServiceCall) -> None:
        """Calculate when to charge."""
        _LOGGER.info("Charge-calculator START")
        _LOGGER.debug("Received service call data=%s", call.data)

        # Safe helpers to read config with defaults
        def cfg_get(path: List[str], default=None):
            node = cfg
            for p in path:
                if not isinstance(node, dict):
                    return default
                node = node.get(p)
                if node is None:
                    return default
            return node

        nordpol_entity = cfg_get(['nordpol_entity'])
        wether_entity = cfg_get(['wether_entity'])
        car_sensor_id = cfg_get(['car_battery', 'sensor_id'])
        house_sensor_id = cfg_get(['house_battery', 'sensor_id'])

        _LOGGER.debug("Config: nordpol=%s, wether=%s, car_sensor=%s, house_sensor=%s", nordpol_entity, wether_entity, car_sensor_id, house_sensor_id)

        # Resolve sensor states
        def get_state_safe(entity_id: Optional[str]):
            if not entity_id:
                return None
            st = hass.states.get(entity_id)
            if st is None:
                _LOGGER.error("Could not get state of sensor: %s", entity_id)
            return st

        car_battery_state = get_state_safe(car_sensor_id)
        house_battery_state = get_state_safe(house_sensor_id)
        nordpol_state = get_state_safe(nordpol_entity)

        if nordpol_state is None:
            _LOGGER.error("Nordpol state is required, aborting calculation.")
            return
        if car_battery_state is None and house_battery_state is None:
            _LOGGER.error("Neither car nor house battery state available, aborting calculation.")
            return

        _LOGGER.debug("nordpol state=%s", nordpol_state)

        # Current time in UTC-aware datetime
        time_now = dt_util.utcnow()
        _LOGGER.debug("Time now (utc)=%s", time_now)

        # Parse optional overrides from service call, fall back to defaults
        try:
            car_charge_effect = float(call.data.get('car_charge_effect', DEFAULTS['car_charge_effect']))
        except (TypeError, ValueError):
            car_charge_effect = DEFAULTS['car_charge_effect']
            _LOGGER.warning("Invalid car_charge_effect provided, using default %s", car_charge_effect)

        try:
            house_charge_effect = float(call.data.get('house_charge_effect', DEFAULTS['house_charge_effect']))
        except (TypeError, ValueError):
            house_charge_effect = DEFAULTS['house_charge_effect']
            _LOGGER.warning("Invalid house_charge_effect provided, using default %s", house_charge_effect)

        try:
            car_charge_stop = int(call.data.get('car_charge_stop', DEFAULTS['car_charge_stop']))
        except (TypeError, ValueError):
            car_charge_stop = DEFAULTS['car_charge_stop']
            _LOGGER.warning("Invalid car_charge_stop provided, using default %s", car_charge_stop)

        try:
            house_charge_stop = int(call.data.get('house_charge_stop', DEFAULTS['house_charge_stop']))
        except (TypeError, ValueError):
            house_charge_stop = DEFAULTS['house_charge_stop']
            _LOGGER.warning("Invalid house_charge_stop provided, using default %s", house_charge_stop)

        def parse_percentage_state(state) -> Optional[float]:
            """Return percentage as float (0-100) or None if not parseable."""
            if state is None:
                return None
            try:
                return float(state.state)
            except (ValueError, TypeError):
                _LOGGER.error("Unable to parse state '%s' for entity %s", state.state if hasattr(state, 'state') else state, getattr(state, 'entity_id', '<unknown>'))
                return None

        car_pct = parse_percentage_state(car_battery_state)
        house_pct = parse_percentage_state(house_battery_state)

        # Helper to compute charge time (in hours), returns 0 when not computable
        def compute_charge_time(current_pct: Optional[float], size_cfg_path: List[str], stop_pct: int, min_time_cfg_path: List[str], effect: float) -> int:
            if current_pct is None:
                return 0
            try:
                size = int(cfg_get(size_cfg_path, 0))
            except (TypeError, ValueError):
                _LOGGER.error("Invalid battery size in config for %s", size_cfg_path)
                return 0
            current_energy = (current_pct / 100.0) * size
            target_energy = (stop_pct / 100.0) * size
            hours = (target_energy - current_energy) / float(effect) if effect > 0 else 0
            hours = max(hours, 0)
            hours_rounded = math.ceil(hours) if hours > 0 else 0
            min_time = int(cfg_get(min_time_cfg_path, 0) or 0)
            if hours_rounded < min_time:
                _LOGGER.debug("Rounded hours %s < min_time %s, using min_time", hours_rounded, min_time)
                return min_time
            return hours_rounded

        # Calculate times (hours) for car and house
        car_hours = compute_charge_time(car_pct, ['car_battery', 'size'], car_charge_stop, ['car_battery', 'min_charge_time'], car_charge_effect)
        house_hours = compute_charge_time(house_pct, ['house_battery', 'size'], house_charge_stop, ['house_battery', 'min_charge_time'], house_charge_effect)

        _LOGGER.info("Calculated charge hours: car=%s, house=%s", car_hours, house_hours)

        # Helper to convert start/stop (may be datetime or ISO string) to timestamp
        def to_timestamp(value) -> Optional[float]:
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, datetime.datetime):
                return dt_util.as_timestamp(value)
            # try parse string
            dt = dt_util.parse_datetime(str(value))
            if dt is None:
                _LOGGER.error("Unable to parse datetime '%s' to timestamp", value)
                return None
            return dt_util.as_timestamp(dt)

        # Generic routine to calculate best time and set hass states
        def process_battery(hours: int, label: str):
            if hours <= 0:
                _LOGGER.info("No charge needed for %s (hours=%s)", label, hours)
                return
            # existing code used *4 when constructing ChargeCalculator, keep same behaviour (periods granularity)
            charge_periods = hours * 4
            cc = ChargeCalculator(_LOGGER, nordpol_state, time_now, charge_periods)
            best = cc.get_best_time_to_charge()
            _LOGGER.debug("Best time for %s = %s", label, best)
            if not best:
                _LOGGER.warning("No best time found for %s", label)
                return
            ts_start = to_timestamp(best.get('start'))
            ts_stop = to_timestamp(best.get('stop'))
            if ts_start is not None:
                hass.states.async_set(f"{DOMAIN}.{label}_start_time", ts_start)
                _LOGGER.info("Entity '%s.%s_start_time' updated: %s", DOMAIN, label, ts_start)
            if ts_stop is not None:
                hass.states.async_set(f"{DOMAIN}.{label}_stop_time", ts_stop)
                _LOGGER.info("Entity '%s.%s_stop_time' updated: %s", DOMAIN, label, ts_stop)

        process_battery(car_hours, "car")
        process_battery(house_hours, "house")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'calculate_charge', calculate_charge_time)

    return True


class ChargeCalculator:
    """Helper to find the best continuous period (by average price) to charge."""

    def __init__(self, logger: logging.Logger, nordpol_state: Any, time_now: datetime.datetime, charge_periods: int):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = getattr(nordpol_state, "attributes", {}) or {}
        self.time_now = time_now
        self.charge_period = int(charge_periods)
        # normalize and filter price periods up-front
        self.aapp = self.next_day_pp_filter(self.get_all_available_price_periods())
        self.logger.debug("Time_now = %s", self.time_now)
        self.logger.debug("charge_period = %s", self.charge_period)

    # --- Helpers to normalize / validate price periods ---
    def _ensure_dt(self, value) -> Optional[datetime.datetime]:
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, (int, float)):
            try:
                return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
            except Exception:
                return None
        return dt_util.parse_datetime(str(value))

    def _normalize_period(self, period: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Ensure a period dict has datetime start/end and float value. Return None if invalid."""
        try:
            start = self._ensure_dt(period.get('start'))
            end = self._ensure_dt(period.get('end'))
            value = period.get('value')
            if start is None or end is None:
                self.logger.debug("Skipping period with invalid start/end: %s", period)
                return None
            # Try to coerce value to float
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                self.logger.debug("Skipping period with invalid value: %s", period)
                return None
            return {'start': start, 'end': end, 'value': value_f}
        except Exception as ex:
            self.logger.exception("Error normalizing period %s: %s", period, ex)
            return None

    def filter_past_prices(self, prices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fp: List[Dict[str, Any]] = []
        for price in prices:
            end = self._ensure_dt(price.get('end'))
            if end and end > self.time_now:
                fp.append(price)
            else:
                self.logger.debug("filter_past_prices: price is in the past or invalid: %s", price)
        return fp

    def next_day_pp_filter(self, prices: List[Dict[str, Any]], hour: int = 11, minute: int = 0, second: int = 0) -> List[Dict[str, Any]]:
        fp: List[Dict[str, Any]] = []
        try:
            cutoff = (self.time_now + datetime.timedelta(days=1)).replace(hour=hour, minute=minute, second=second, microsecond=0)
        except Exception:
            cutoff = self.time_now + datetime.timedelta(days=1)
        self.logger.debug("CUTOFF = %s", cutoff)

        for price in prices:
            if price.get('end') and price['end'] < cutoff:
                fp.append(price)
            else:
                self.logger.debug("next_day_pp_filter: price is after cutoff or invalid: %s", price)
        return fp

    def isfloat(self, num) -> bool:
        try:
            if num is None:
                return False
            float(num)
            return True
        except (ValueError, TypeError):
            return False

    def validate_price(self, price_periods: List[Dict[str, Any]]) -> bool:
        for price in price_periods:
            if not self.isfloat(price.get('value')):
                return False
        return True

    def get_all_available_price_periods(self) -> List[Dict[str, Any]]:
        raw_today = self.nordpol_attributes.get('raw_today', []) or []
        raw_tomorrow = self.nordpol_attributes.get('raw_tomorrow', []) or []
        combined: List[Dict[str, Any]] = []

        for raw in (raw_today, raw_tomorrow):
            # normalize each period and validate
            for p in raw:
                norm = self._normalize_period(p)
                if norm:
                    combined.append(norm)

        # filter out past periods
        combined = self.filter_past_prices(combined)
        # Sort by end date ascending
        combined.sort(key=lambda x: x['end'])
        return combined

    def calc_average_charge_price(self, aapp: List[Dict[str, Any]], charge_period: int) -> List[Dict[str, Any]]:
        average_charge_prices: List[Dict[str, Any]] = []
        if charge_period <= 0:
            return average_charge_prices
        for i in range(len(aapp)):
            if i + charge_period > len(aapp):
                break
            sum_price = 0.0
            periods = []
            for cp in range(charge_period):
                idx = i + cp
                sum_price += aapp[idx]['value']
                periods.append(aapp[idx])
            avg = sum_price / charge_period
            self.logger.debug("sum_price/charge_period: %s/%s -> avg=%s", sum_price, charge_period, avg)
            average_charge_prices.append({'value': avg, 'periods': periods})
        return average_charge_prices

    def get_lowest_average_charge_period(self, aapp: List[Dict[str, Any]], charge_period: int) -> Optional[Dict[str, Any]]:
        average_charge_prices = self.calc_average_charge_price(aapp, charge_period)
        average_charge_prices.sort(key=lambda x: x['value'])
        self.print_average_charge_periods(average_charge_prices)
        if average_charge_prices:
            self.logger.info("Best charge period: %s", average_charge_prices[0])
            return average_charge_prices[0]
        return None

    def print_price_periods(self, price_periods: List[Dict[str, Any]]):
        self.logger.info("Print_price_periods:")
        for price_period in price_periods:
            try:
                self.logger.info("Start=%s, End=%s, Value=%s",
                                 price_period['start'].strftime('%Y-%m-%d %H:%M'),
                                 price_period['end'].strftime('%Y-%m-%d %H:%M'),
                                 price_period['value'])
            except Exception:
                self.logger.debug("Unable to pretty-print price period: %s", price_period)

    def print_average_charge_periods(self, average_charge_periods: List[Dict[str, Any]]):
        self.logger.debug("Print_average_charge_periods:")
        for period in average_charge_periods:
            try:
                self.logger.debug("Start=%s, End=%s, Value=%s",
                                  period['periods'][0]['start'].strftime('%Y-%m-%d %H:%M'),
                                  period['periods'][-1]['end'].strftime('%Y-%m-%d %H:%M'),
                                  period['value'])
            except Exception:
                self.logger.debug("Unable to pretty-print average period: %s", period)

    def get_best_time_to_charge(self) -> Dict[str, Any]:
        best_charge_period = self.get_lowest_average_charge_period(self.aapp, self.charge_period)
        if best_charge_period:
            self.print_price_periods(best_charge_period['periods'])
            self.logger.info("get_best_time_to_charge: %s - %s", best_charge_period['periods'][0]['start'], best_charge_period['periods'][-1]['end'])
            return {"start": best_charge_period['periods'][0]['start'], "stop": best_charge_period['periods'][-1]['end']}
        return {}

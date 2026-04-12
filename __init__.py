"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
import datetime
import math
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "car_charge_effect": 6.6,
    "house_charge_effect": 4.0,
    "car_charge_stop": 80,
    "house_charge_stop": 90,
    "car_max_sessions": 1,
    "house_max_sessions": 1,
    "interval_minutes": 5,
}


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _coerce_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
        try:
            return int(stripped)
        except ValueError:
            return value

    try:
        return float(stripped)
    except ValueError:
        return value


def _render_service_data(value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_service_data(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_service_data(item, context) for item in value]
    if isinstance(value, str):
        rendered = value.format_map(_SafeFormatDict(context))
        return _coerce_value(rendered)
    return value


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the async service charge_calculator."""
    cfg = config.get(DOMAIN, {})
    runtime = hass.data.setdefault(DOMAIN, {})
    runtime.setdefault("last_triggered_start", {})

    if not cfg:
        _LOGGER.warning("No configuration found for domain '%s'. Service will still be available.", DOMAIN)

    def cfg_get(path: List[str], default=None):
        node = cfg
        for part in path:
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def get_state_safe(entity_id: Optional[str]):
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Could not get state of sensor: %s", entity_id)
        return state

    def parse_percentage_state(state) -> Optional[float]:
        if state is None:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.error(
                "Unable to parse state '%s' for entity %s",
                state.state if hasattr(state, "state") else state,
                getattr(state, "entity_id", "<unknown>"),
            )
            return None

    def compute_charge_time(
        current_pct: Optional[float],
        size_cfg_path: List[str],
        stop_pct: int,
        min_time_cfg_path: List[str],
        effect: float,
    ) -> int:
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

    def to_timestamp(value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime.datetime):
            return dt_util.as_timestamp(value)
        parsed_dt = dt_util.parse_datetime(str(value))
        if parsed_dt is None:
            _LOGGER.error("Unable to parse datetime '%s' to timestamp", value)
            return None
        return dt_util.as_timestamp(parsed_dt)

    def clear_schedule_entities(label: str) -> None:
        hass.states.async_remove(f"{DOMAIN}.{label}_start_time")
        hass.states.async_remove(f"{DOMAIN}.{label}_stop_time")

    async def execute_battery_action(schedule: Dict[str, Any], time_now: datetime.datetime) -> None:
        label = schedule["label"]
        action_cfg = cfg_get([f"{label}_charge_action"], {}) or {}
        service = action_cfg.get("service")

        if not service:
            return
        if not isinstance(service, str) or "." not in service:
            _LOGGER.error("Invalid service configured for %s_charge_action: %s", label, service)
            return

        ts_start = schedule.get("start_ts")
        ts_stop = schedule.get("stop_ts")
        now_ts = dt_util.as_timestamp(time_now)
        if ts_start is None or ts_stop is None or now_ts < ts_start or now_ts >= ts_stop:
            return

        last_triggered = runtime["last_triggered_start"].get(label)
        if last_triggered == ts_start:
            _LOGGER.debug("Action for %s already triggered for start %s", label, ts_start)
            return

        action_data = action_cfg.get("data", {}) or {}
        if not isinstance(action_data, dict):
            _LOGGER.error("Configured action data for %s must be a dictionary", label)
            return

        service_domain, service_name = service.split(".", 1)
        context = {
            "label": label,
            "session_index": schedule.get("session_index", 1),
            "start": schedule["start"].isoformat(),
            "stop": schedule["stop"].isoformat(),
            "start_ts": ts_start,
            "stop_ts": ts_stop,
            "charge_hours": schedule["charge_hours"],
            "stop_pct": schedule["stop_pct"],
            "current_pct": schedule.get("current_pct"),
            "charge_power_kw": schedule["charge_power_kw"],
            "charge_power_w": int(schedule["charge_power_kw"] * 1000),
        }

        rendered_data = _render_service_data(action_data, context)
        await hass.services.async_call(service_domain, service_name, rendered_data, blocking=True)
        runtime["last_triggered_start"][label] = ts_start
        _LOGGER.info("Triggered %s charging through %s with data=%s", label, service, rendered_data)

    async def handle_charge_calculation(
        call: Optional[ServiceCall] = None,
        *,
        execute_actions: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        _LOGGER.info("Charge-calculator START")
        if call is not None:
            _LOGGER.debug("Received service call data=%s", call.data)

        nordpol_entity = cfg_get(["nordpol_entity"])
        wether_entity = cfg_get(["wether_entity"])
        car_sensor_id = cfg_get(["car_battery", "sensor_id"])
        house_sensor_id = cfg_get(["house_battery", "sensor_id"])

        _LOGGER.debug(
            "Config: nordpol=%s, wether=%s, car_sensor=%s, house_sensor=%s",
            nordpol_entity,
            wether_entity,
            car_sensor_id,
            house_sensor_id,
        )

        car_battery_state = get_state_safe(car_sensor_id)
        house_battery_state = get_state_safe(house_sensor_id)
        nordpol_state = get_state_safe(nordpol_entity)

        if nordpol_state is None:
            _LOGGER.error("Nordpol state is required, aborting calculation.")
            return {}
        if car_battery_state is None and house_battery_state is None:
            _LOGGER.error("Neither car nor house battery state available, aborting calculation.")
            return {}

        time_now = dt_util.utcnow()
        _LOGGER.debug("Time now (utc)=%s", time_now)

        call_data = call.data if call else {}

        try:
            car_charge_effect = float(call_data.get("car_charge_effect", DEFAULTS["car_charge_effect"]))
        except (TypeError, ValueError):
            car_charge_effect = DEFAULTS["car_charge_effect"]
            _LOGGER.warning("Invalid car_charge_effect provided, using default %s", car_charge_effect)

        try:
            house_charge_effect = float(call_data.get("house_charge_effect", DEFAULTS["house_charge_effect"]))
        except (TypeError, ValueError):
            house_charge_effect = DEFAULTS["house_charge_effect"]
            _LOGGER.warning("Invalid house_charge_effect provided, using default %s", house_charge_effect)

        try:
            car_charge_stop = int(call_data.get("car_charge_stop", DEFAULTS["car_charge_stop"]))
        except (TypeError, ValueError):
            car_charge_stop = DEFAULTS["car_charge_stop"]
            _LOGGER.warning("Invalid car_charge_stop provided, using default %s", car_charge_stop)

        try:
            house_charge_stop = int(call_data.get("house_charge_stop", DEFAULTS["house_charge_stop"]))
        except (TypeError, ValueError):
            house_charge_stop = DEFAULTS["house_charge_stop"]
            _LOGGER.warning("Invalid house_charge_stop provided, using default %s", house_charge_stop)

        def parse_positive_int(value: Any, default: int, label: str) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                _LOGGER.warning("Invalid %s provided, using default %s", label, default)
                return default
            if parsed <= 0:
                _LOGGER.warning("%s must be positive, using default %s", label, default)
                return default
            return parsed

        car_max_sessions = parse_positive_int(
            call_data.get("car_max_sessions", cfg_get(["car_battery", "max_sessions"], DEFAULTS["car_max_sessions"])),
            DEFAULTS["car_max_sessions"],
            "car_max_sessions",
        )
        house_max_sessions = parse_positive_int(
            call_data.get("house_max_sessions", cfg_get(["house_battery", "max_sessions"], DEFAULTS["house_max_sessions"])),
            DEFAULTS["house_max_sessions"],
            "house_max_sessions",
        )

        car_pct = parse_percentage_state(car_battery_state)
        house_pct = parse_percentage_state(house_battery_state)

        car_hours = compute_charge_time(
            car_pct,
            ["car_battery", "size"],
            car_charge_stop,
            ["car_battery", "min_charge_time"],
            car_charge_effect,
        )
        house_hours = compute_charge_time(
            house_pct,
            ["house_battery", "size"],
            house_charge_stop,
            ["house_battery", "min_charge_time"],
            house_charge_effect,
        )

        _LOGGER.info("Calculated charge hours: car=%s, house=%s", car_hours, house_hours)

        def process_battery(
            *,
            hours: int,
            label: str,
            stop_pct: int,
            current_pct: Optional[float],
            charge_effect: float,
            max_sessions: int,
        ) -> List[Dict[str, Any]]:
            if hours <= 0:
                _LOGGER.info("No charge needed for %s (hours=%s)", label, hours)
                clear_schedule_entities(label)
                return []

            charge_periods = hours * 4
            charge_calculator = ChargeCalculator(_LOGGER, nordpol_state, time_now, charge_periods)
            windows = charge_calculator.get_best_time_windows(
                total_periods=charge_periods,
                max_windows=max_sessions,
            )

            if not windows:
                _LOGGER.warning("No best time windows found for %s", label)
                clear_schedule_entities(label)
                return []

            schedules: List[Dict[str, Any]] = []
            for index, window in enumerate(windows, start=1):
                start = window.get("start")
                stop = window.get("stop")
                ts_start = to_timestamp(start)
                ts_stop = to_timestamp(stop)
                if ts_start is None or ts_stop is None:
                    _LOGGER.warning("Unable to convert calculated schedule to timestamps for %s", label)
                    continue

                schedules.append(
                    {
                        "label": label,
                        "session_index": index,
                        "start": start,
                        "stop": stop,
                        "start_ts": ts_start,
                        "stop_ts": ts_stop,
                        "charge_hours": hours,
                        "stop_pct": stop_pct,
                        "current_pct": current_pct,
                        "charge_power_kw": charge_effect,
                    }
                )

            if not schedules:
                clear_schedule_entities(label)
                return []

            active_or_next = next(
                (item for item in schedules if item["start_ts"] <= dt_util.as_timestamp(time_now) < item["stop_ts"]),
                None,
            )
            if active_or_next is None:
                active_or_next = next(
                    (item for item in schedules if item["start_ts"] >= dt_util.as_timestamp(time_now)),
                    schedules[0],
                )

            hass.states.async_set(f"{DOMAIN}.{label}_start_time", active_or_next["start_ts"])
            hass.states.async_set(f"{DOMAIN}.{label}_stop_time", active_or_next["stop_ts"])
            _LOGGER.info(
                "Entity '%s.%s_start_time' updated: %s",
                DOMAIN,
                label,
                active_or_next["start_ts"],
            )
            _LOGGER.info(
                "Entity '%s.%s_stop_time' updated: %s",
                DOMAIN,
                label,
                active_or_next["stop_ts"],
            )

            return schedules

        schedules = {
            "car": process_battery(
                hours=car_hours,
                label="car",
                stop_pct=car_charge_stop,
                current_pct=car_pct,
                charge_effect=car_charge_effect,
                max_sessions=car_max_sessions,
            ),
            "house": process_battery(
                hours=house_hours,
                label="house",
                stop_pct=house_charge_stop,
                current_pct=house_pct,
                charge_effect=house_charge_effect,
                max_sessions=house_max_sessions,
            ),
        }

        if execute_actions:
            for schedule_group in schedules.values():
                for schedule in schedule_group:
                    await execute_battery_action(schedule, time_now)

        return {key: value for key, value in schedules.items() if value}

    async def calculate_charge_time(call: ServiceCall) -> None:
        await handle_charge_calculation(
            call,
            execute_actions=bool(call.data.get("execute_actions", False)),
        )

    interval_minutes = cfg_get(["interval_minutes"], DEFAULTS["interval_minutes"])
    try:
        interval_minutes = int(interval_minutes)
    except (TypeError, ValueError):
        interval_minutes = DEFAULTS["interval_minutes"]
        _LOGGER.warning("Invalid interval_minutes configured, using default %s", interval_minutes)
    if interval_minutes <= 0:
        interval_minutes = DEFAULTS["interval_minutes"]
        _LOGGER.warning("interval_minutes must be positive, using default %s", interval_minutes)

    @callback
    def schedule_periodic_evaluation(_: datetime.datetime) -> None:
        hass.async_create_task(handle_charge_calculation(execute_actions=True))

    if cfg:
        runtime["periodic_unsubscribe"] = async_track_time_interval(
            hass,
            schedule_periodic_evaluation,
            datetime.timedelta(minutes=interval_minutes),
        )
        hass.async_create_task(handle_charge_calculation(execute_actions=True))

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

    def get_best_time_windows(self, total_periods: int, max_windows: int = 1) -> List[Dict[str, Any]]:
        if total_periods <= 0:
            return []

        available = list(self.aapp)
        selected: List[Dict[str, Any]] = []
        remaining_periods = total_periods
        remaining_windows = max(1, int(max_windows))

        while remaining_periods > 0 and available:
            periods_in_window = max(1, math.ceil(remaining_periods / remaining_windows))
            best = self.get_lowest_average_charge_period(available, periods_in_window)

            if best is None and periods_in_window > 1:
                periods_in_window = 1
                best = self.get_lowest_average_charge_period(available, periods_in_window)

            if best is None:
                break

            selected.append(best)

            used = {(p['start'], p['end']) for p in best['periods']}
            available = [p for p in available if (p['start'], p['end']) not in used]
            available.sort(key=lambda x: x['end'])

            remaining_periods -= len(best['periods'])
            remaining_windows -= 1
            if remaining_windows <= 0:
                remaining_windows = 1

        windows: List[Dict[str, Any]] = []
        for period in selected:
            windows.append(
                {
                    "start": period['periods'][0]['start'],
                    "stop": period['periods'][-1]['end'],
                    "avg": period['value'],
                    "period_count": len(period['periods']),
                }
            )

        windows.sort(key=lambda x: x["start"])
        self.logger.info("Selected %s charging windows", len(windows))
        for window in windows:
            self.logger.info("Window start=%s stop=%s avg=%s periods=%s", window["start"], window["stop"], window["avg"], window["period_count"])
        return windows

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
        windows = self.get_best_time_windows(self.charge_period, max_windows=1)
        if windows:
            self.logger.info("get_best_time_to_charge: %s - %s", windows[0]['start'], windows[0]['stop'])
            return {"start": windows[0]['start'], "stop": windows[0]['stop']}
        return {}

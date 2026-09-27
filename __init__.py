"""Custom component Charge Calculator."""
from __future__ import annotations
import logging
import datetime
import math
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    LABELS,
    SIGNAL_PLAN_UPDATED,
    STATUS_CHARGING,
    STATUS_IDLE,
    STATUS_NOT_PROFITABLE,
    STATUS_NO_WINDOW,
    STATUS_SCHEDULED,
    STATUS_UNAVAILABLE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "car_charge_effect": 6.6,
    "house_charge_effect": 4.0,
    "car_charge_stop": 80,
    "house_charge_stop": 90,
    "car_max_sessions": 1,
    "house_max_sessions": 1,
    "interval_minutes": 5,
    "min_session_minutes": 60,
    "min_saving_ratio": 0.03,
    "round_trip_efficiency": 0.9,
    "cycle_cost": 0.0,
    "event_throttle_seconds": 60,
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


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_iso(value: Any) -> Optional[str]:
    """Accepts a timestamp or datetime and returns local ISO time."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = dt_util.utc_from_timestamp(float(value))
    return dt_util.as_local(value).isoformat()


def _local_hm(value: Any) -> str:
    """Short local time for log lines."""
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        value = dt_util.utc_from_timestamp(float(value))
    return dt_util.as_local(value).strftime("%a %H:%M")


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

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored = await store.async_load() or {}
    runtime["sessions"] = stored.get("sessions", {}) or {}
    runtime["store"] = store
    runtime.setdefault("plans", {})
    runtime.setdefault("prices", [])
    runtime.setdefault("price_stats", {})

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

    def compute_charge_hours(
        current_pct: Optional[float],
        size_cfg_path: List[str],
        stop_pct: int,
        min_time_cfg_path: List[str],
        effect: float,
    ) -> float:
        if current_pct is None:
            return 0.0
        try:
            size = float(cfg_get(size_cfg_path, 0))
        except (TypeError, ValueError):
            _LOGGER.error("Invalid battery size in config for %s", size_cfg_path)
            return 0.0

        missing_energy = ((stop_pct - current_pct) / 100.0) * size
        hours = missing_energy / float(effect) if effect > 0 else 0.0
        if hours <= 0:
            return 0.0

        try:
            min_time = float(cfg_get(min_time_cfg_path, 0) or 0)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid min_charge_time in config for %s", min_time_cfg_path)
            min_time = 0.0
        return max(hours, min_time)

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

    @callback
    def publish_plan(label: str, status: str, **details: Any) -> None:
        plan = {
            "label": label,
            "status": status,
            "updated_at": _local_iso(dt_util.utcnow()),
            "sessions": [],
            "next_start": None,
            "next_stop": None,
            **details,
        }
        runtime["plans"][label] = plan
        async_dispatcher_send(hass, SIGNAL_PLAN_UPDATED, label)

    def describe_sessions(schedules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "index": item["session_index"],
                "start": _local_iso(item["start_ts"]),
                "stop": _local_iso(item["stop_ts"]),
                "hours": item["charge_hours"],
                "avg_price": item.get("avg_price"),
            }
            for item in schedules
        ]

    @callback
    def publish_charging_plan(active: Dict[str, Any], now_ts: float, reason: str) -> None:
        publish_plan(
            active["label"],
            STATUS_CHARGING,
            reason=reason,
            sessions=describe_sessions([active]),
            next_start=_local_iso(active["start_ts"]),
            next_stop=_local_iso(active["stop_ts"]),
            session_count=1,
            current_pct=active.get("current_pct"),
            target_pct=active.get("stop_pct"),
            charge_power_kw=active.get("charge_power_kw"),
            average_price=active.get("avg_price"),
            minutes_remaining=round((active["stop_ts"] - now_ts) / 60),
        )

    async def save_sessions() -> None:
        await store.async_save({"sessions": runtime["sessions"]})

    def build_action_context(schedule: Dict[str, Any]) -> Dict[str, Any]:
        start = dt_util.as_local(dt_util.utc_from_timestamp(schedule["start_ts"]))
        stop = dt_util.as_local(dt_util.utc_from_timestamp(schedule["stop_ts"]))
        power_kw = float(schedule.get("charge_power_kw") or 0)
        return {
            "label": schedule["label"],
            "session_index": schedule.get("session_index", 1),
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "start_ts": schedule["start_ts"],
            "stop_ts": schedule["stop_ts"],
            "charge_hours": schedule.get("charge_hours", 0),
            "stop_pct": schedule.get("stop_pct"),
            "current_pct": schedule.get("current_pct"),
            "charge_power_kw": power_kw,
            "charge_power_w": int(power_kw * 1000),
            "avg_price": schedule.get("avg_price"),
        }

    async def call_charge_action(schedule: Dict[str, Any], kind: str) -> bool:
        label = schedule["label"]
        key = f"{label}_charge_action" if kind == "start" else f"{label}_charge_stop_action"
        action_cfg = cfg_get([key], {}) or {}
        service = action_cfg.get("service")

        if not service:
            _LOGGER.debug("No %s action configured for %s", kind, label)
            return False
        if not isinstance(service, str) or "." not in service:
            _LOGGER.error("Invalid service configured for %s: %s", key, service)
            return False

        action_data = action_cfg.get("data", {}) or {}
        if not isinstance(action_data, dict):
            _LOGGER.error("Configured action data for %s must be a dictionary", key)
            return False

        service_domain, service_name = service.split(".", 1)
        rendered_data = _render_service_data(action_data, build_action_context(schedule))
        await hass.services.async_call(service_domain, service_name, rendered_data, blocking=True)
        _LOGGER.info(
            "ACTION %s %s: called %s (window %s -> %s, %.2f kW, target %s%%, avg price %s) data=%s",
            kind.upper(),
            label,
            service,
            _local_hm(schedule["start_ts"]),
            _local_hm(schedule["stop_ts"]),
            float(schedule.get("charge_power_kw") or 0),
            schedule.get("stop_pct"),
            schedule.get("avg_price"),
            rendered_data,
        )
        return True

    async def reconcile_active_session(label: str, now_ts: float) -> Optional[Dict[str, Any]]:
        """Return the pinned session while it runs, stopping it once the window has passed."""
        active = runtime["sessions"].get(label)
        if not active:
            return None

        if now_ts >= active["stop_ts"]:
            _LOGGER.info(
                "%s session finished (%s -> %s), stopping charge",
                label,
                _local_hm(active["start_ts"]),
                _local_hm(active["stop_ts"]),
            )
            await call_charge_action(active, "stop")
            runtime["sessions"].pop(label, None)
            await save_sessions()
            return None

        if now_ts < active["start_ts"]:
            _LOGGER.debug("Discarding stale future session for %s", label)
            runtime["sessions"].pop(label, None)
            await save_sessions()
            return None

        return active

    async def start_due_session(schedules: List[Dict[str, Any]], now_ts: float) -> None:
        due = next(
            (item for item in schedules if item["start_ts"] <= now_ts < item["stop_ts"]),
            None,
        )
        if due is None:
            return

        label = due["label"]
        if runtime["sessions"].get(label, {}).get("start_ts") == due["start_ts"]:
            return

        if await call_charge_action(due, "start"):
            runtime["sessions"][label] = due
            await save_sessions()

    async def handle_charge_calculation(
        call: Optional[ServiceCall] = None,
        *,
        execute_actions: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        _LOGGER.info("--- Charge calculator run (execute_actions=%s) ---", execute_actions)
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
            for label in LABELS:
                publish_plan(label, STATUS_UNAVAILABLE, reason=f"Price entity {nordpol_entity} unavailable")
            return {}
        if car_battery_state is None and house_battery_state is None:
            _LOGGER.error("Neither car nor house battery state available, aborting calculation.")
            for label in LABELS:
                publish_plan(label, STATUS_UNAVAILABLE, reason="No battery state-of-charge sensor available")
            return {}

        time_now = dt_util.utcnow()
        _LOGGER.debug("Time now (utc)=%s", time_now)

        calculator = ChargeCalculator(_LOGGER, nordpol_state, time_now)
        if not calculator.aapp:
            _LOGGER.error("No usable price periods available, aborting calculation.")
            for label in LABELS:
                publish_plan(label, STATUS_UNAVAILABLE, reason="No usable price periods")
            return {}

        price_stats = calculator.price_stats()
        runtime["price_stats"] = price_stats
        runtime["prices"] = [
            {"start": _local_iso(period["start"]), "value": round(period["value"], 4)}
            for period in calculator.aapp
        ]
        _LOGGER.info(
            "Prices: %s periods of %s min until %s (tomorrow_valid=%s) | min %.4f @ %s, max %.4f @ %s, avg %.4f",
            price_stats["count"],
            calculator.period_minutes,
            _local_hm(price_stats["horizon_end"]),
            calculator.tomorrow_valid,
            price_stats["min"],
            _local_hm(price_stats["min_at"]),
            price_stats["max"],
            _local_hm(price_stats["max_at"]),
            price_stats["avg"],
        )

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

        car_hours = compute_charge_hours(
            car_pct,
            ["car_battery", "size"],
            car_charge_stop,
            ["car_battery", "min_charge_time"],
            car_charge_effect,
        )
        house_hours = compute_charge_hours(
            house_pct,
            ["house_battery", "size"],
            house_charge_stop,
            ["house_battery", "min_charge_time"],
            house_charge_effect,
        )

        def process_battery(
            *,
            hours: float,
            label: str,
            stop_pct: int,
            current_pct: Optional[float],
            charge_effect: float,
            max_sessions: int,
        ) -> List[Dict[str, Any]]:
            base_details = {
                "current_pct": current_pct,
                "target_pct": stop_pct,
                "charge_power_kw": charge_effect,
                "hours_needed": round(hours, 2),
                "max_sessions": max_sessions,
                "price_min": price_stats["min"],
                "price_max": price_stats["max"],
                "price_avg": round(price_stats["avg"], 4),
                "horizon_end": _local_iso(price_stats["horizon_end"]),
                "tomorrow_prices_available": calculator.tomorrow_valid,
            }

            if hours <= 0:
                _LOGGER.info(
                    "%s: no charge needed (soc=%s%%, target=%s%%)", label, current_pct, stop_pct
                )
                clear_schedule_entities(label)
                publish_plan(label, STATUS_IDLE, reason="Target state of charge already reached", **base_details)
                return []

            charge_periods = calculator.periods_for_hours(hours)
            battery_cfg = [f"{label}_battery"]
            min_window_periods = calculator.periods_for_minutes(
                cfg_get(battery_cfg + ["min_session_minutes"], DEFAULTS["min_session_minutes"])
            )
            min_saving_ratio = _as_float(
                cfg_get(battery_cfg + ["min_saving_ratio"], DEFAULTS["min_saving_ratio"]),
                DEFAULTS["min_saving_ratio"],
            )

            _LOGGER.info(
                "%s: soc=%s%% target=%s%% -> %.2f h (%s x %s min periods) at %.1f kW, max %s session(s)",
                label,
                current_pct,
                stop_pct,
                hours,
                charge_periods,
                calculator.period_minutes,
                charge_effect,
                max_sessions,
            )

            windows = calculator.get_best_time_windows(
                total_periods=charge_periods,
                max_windows=max_sessions,
                min_window_periods=min_window_periods,
                min_saving_ratio=min_saving_ratio,
            )

            stats = calculator.last_plan_stats
            base_details.update(
                {
                    "periods_needed": charge_periods,
                    "plan_cost": stats.get("cost"),
                    "baseline_cost": stats.get("baseline_cost"),
                    "saving_vs_single_window": stats.get("saving"),
                }
            )

            if not windows:
                _LOGGER.warning("%s: no charging window fits in the available price horizon", label)
                clear_schedule_entities(label)
                publish_plan(label, STATUS_NO_WINDOW, reason="No window fits in the price horizon", **base_details)
                return []

            if cfg_get(battery_cfg + ["break_even"], False):
                efficiency = _as_float(
                    cfg_get(battery_cfg + ["round_trip_efficiency"], DEFAULTS["round_trip_efficiency"]),
                    DEFAULTS["round_trip_efficiency"],
                )
                cycle_cost = _as_float(
                    cfg_get(battery_cfg + ["cycle_cost"], DEFAULTS["cycle_cost"]),
                    DEFAULTS["cycle_cost"],
                )
                charge_price = calculator.weighted_average(windows)
                discharge_price = calculator.expected_discharge_price(charge_periods)
                if charge_price is not None and discharge_price is not None and efficiency > 0:
                    effective_price = charge_price / efficiency + cycle_cost
                    base_details.update(
                        {
                            "effective_charge_price": round(effective_price, 4),
                            "expected_discharge_price": round(discharge_price, 4),
                        }
                    )
                    if effective_price >= discharge_price:
                        reason = (
                            f"Not profitable: {charge_price:.4f} / {efficiency:.2f} + {cycle_cost:.4f} "
                            f"= {effective_price:.4f} >= {discharge_price:.4f}"
                        )
                        _LOGGER.info("%s: skipping charge. %s", label, reason)
                        clear_schedule_entities(label)
                        publish_plan(label, STATUS_NOT_PROFITABLE, reason=reason, **base_details)
                        return []
                    _LOGGER.info(
                        "%s: profitable, effective charge %.4f vs expected discharge %.4f",
                        label,
                        effective_price,
                        discharge_price,
                    )

            schedules: List[Dict[str, Any]] = []
            for index, window in enumerate(windows, start=1):
                ts_start = to_timestamp(window.get("start"))
                ts_stop = to_timestamp(window.get("stop"))
                if ts_start is None or ts_stop is None:
                    _LOGGER.warning("Unable to convert calculated schedule to timestamps for %s", label)
                    continue

                schedules.append(
                    {
                        "label": label,
                        "session_index": index,
                        "start_ts": ts_start,
                        "stop_ts": ts_stop,
                        "charge_hours": round(window["period_count"] * calculator.period_hours, 3),
                        "stop_pct": stop_pct,
                        "current_pct": current_pct,
                        "charge_power_kw": charge_effect,
                        "avg_price": round(window["avg"], 4),
                    }
                )

            if not schedules:
                clear_schedule_entities(label)
                publish_plan(label, STATUS_NO_WINDOW, reason="Calculated window had no valid timestamps", **base_details)
                return []

            now_ts = dt_util.as_timestamp(time_now)
            active_or_next = next(
                (item for item in schedules if item["start_ts"] <= now_ts < item["stop_ts"]),
                None,
            )
            if active_or_next is None:
                active_or_next = next(
                    (item for item in schedules if item["start_ts"] >= now_ts),
                    schedules[0],
                )

            hass.states.async_set(f"{DOMAIN}.{label}_start_time", active_or_next["start_ts"])
            hass.states.async_set(f"{DOMAIN}.{label}_stop_time", active_or_next["stop_ts"])

            for item in schedules:
                _LOGGER.info(
                    "%s: session %s/%s %s -> %s (%.2f h, avg %.4f)",
                    label,
                    item["session_index"],
                    len(schedules),
                    _local_hm(item["start_ts"]),
                    _local_hm(item["stop_ts"]),
                    item["charge_hours"],
                    item["avg_price"],
                )
            if stats.get("saving"):
                _LOGGER.info(
                    "%s: %s session(s) cost %.4f vs %.4f for one continuous window (saving %.1f%%)",
                    label,
                    len(schedules),
                    stats["cost"],
                    stats["baseline_cost"],
                    stats["saving"] * 100,
                )

            publish_plan(
                label,
                STATUS_SCHEDULED,
                sessions=describe_sessions(schedules),
                next_start=_local_iso(active_or_next["start_ts"]),
                next_stop=_local_iso(active_or_next["stop_ts"]),
                session_count=len(schedules),
                average_price=round(calculator.weighted_average(windows) or 0, 4),
                **base_details,
            )

            return schedules

        now_ts = dt_util.as_timestamp(time_now)
        schedules: Dict[str, List[Dict[str, Any]]] = {}

        batteries = (
            ("car", car_hours, car_charge_stop, car_pct, car_charge_effect, car_max_sessions),
            ("house", house_hours, house_charge_stop, house_pct, house_charge_effect, house_max_sessions),
        )

        for label, hours, stop_pct, current_pct, charge_effect, max_sessions in batteries:
            active = await reconcile_active_session(label, now_ts) if execute_actions else None
            if active is not None:
                _LOGGER.info(
                    "%s: charging now, window pinned %s -> %s (%.0f min left)",
                    label,
                    _local_hm(active["start_ts"]),
                    _local_hm(active["stop_ts"]),
                    (active["stop_ts"] - now_ts) / 60,
                )
                schedules[label] = [active]
                publish_charging_plan(active, now_ts, "Session in progress, plan pinned until it ends")
                continue

            planned = process_battery(
                hours=hours,
                label=label,
                stop_pct=stop_pct,
                current_pct=current_pct,
                charge_effect=charge_effect,
                max_sessions=max_sessions,
            )
            schedules[label] = planned

            if execute_actions and planned:
                await start_due_session(planned, now_ts)
                started = runtime["sessions"].get(label)
                if started:
                    publish_charging_plan(started, now_ts, "Charging started for this window")

        return {key: value for key, value in schedules.items() if value}

    async def calculate_charge_time(call: ServiceCall) -> Dict[str, Any]:
        return await handle_charge_calculation(
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

    @callback
    def handle_tracked_state_change(_event) -> None:
        now = dt_util.utcnow()
        last_run = runtime.get("last_event_run")
        if last_run is not None and (now - last_run).total_seconds() < DEFAULTS["event_throttle_seconds"]:
            return
        runtime["last_event_run"] = now
        hass.async_create_task(handle_charge_calculation(execute_actions=True))

    if cfg:
        runtime["periodic_unsubscribe"] = async_track_time_interval(
            hass,
            schedule_periodic_evaluation,
            datetime.timedelta(minutes=interval_minutes),
        )

        # Nordpool publishes tomorrow's prices around 13:00; react to that instead of waiting for the timer.
        nordpol_entity = cfg_get(["nordpol_entity"])
        if nordpol_entity:
            runtime["state_unsubscribe"] = async_track_state_change_event(
                hass,
                [nordpol_entity],
                handle_tracked_state_change,
            )

        for platform in ("sensor", "binary_sensor"):
            hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))

        hass.async_create_task(handle_charge_calculation(execute_actions=True))

    # Register our service with Home Assistant.
    hass.services.async_register(
        DOMAIN,
        'calculate_charge',
        calculate_charge_time,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


class ChargeCalculator:
    """Finds the cheapest charging windows in the available Nordpool price horizon."""

    def __init__(self, logger: logging.Logger, nordpol_state: Any, time_now: datetime.datetime):
        self.logger = logger
        self.nordpol_state = nordpol_state
        self.nordpol_attributes = getattr(nordpol_state, "attributes", {}) or {}
        self.time_now = time_now
        self.aapp = self.get_all_available_price_periods()
        self.period_minutes = self._detect_period_minutes()
        self.last_plan_stats: Dict[str, Any] = {}
        self.logger.debug(
            "Horizon: %s periods of %s min, tomorrow_valid=%s, end=%s",
            len(self.aapp),
            self.period_minutes,
            self.tomorrow_valid,
            self.aapp[-1]['end'] if self.aapp else None,
        )

    @property
    def tomorrow_valid(self) -> bool:
        return bool(self.nordpol_attributes.get('tomorrow_valid'))

    @property
    def period_hours(self) -> float:
        return self.period_minutes / 60.0

    def _detect_period_minutes(self) -> int:
        for period in self.aapp:
            minutes = int(round((period['end'] - period['start']).total_seconds() / 60))
            if minutes > 0:
                return minutes
        return 60

    def periods_for_hours(self, hours: float) -> int:
        if hours <= 0:
            return 0
        return max(1, math.ceil((hours * 60) / self.period_minutes))

    def periods_for_minutes(self, minutes: Any) -> int:
        value = _as_float(minutes, 0.0)
        if value <= 0:
            return 1
        return max(1, math.ceil(value / self.period_minutes))

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
        combined.sort(key=lambda x: x['start'])
        return combined

    def _contiguous_blocks(self, periods: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Split a time-sorted period list into runs with no gaps, so windows stay continuous."""
        blocks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for period in sorted(periods, key=lambda x: x['start']):
            if current and period['start'] != current[-1]['end']:
                blocks.append(current)
                current = []
            current.append(period)
        if current:
            blocks.append(current)
        return blocks

    def calc_average_charge_price(self, aapp: List[Dict[str, Any]], charge_period: int) -> List[Dict[str, Any]]:
        average_charge_prices: List[Dict[str, Any]] = []
        if charge_period <= 0:
            return average_charge_prices
        for block in self._contiguous_blocks(aapp):
            for i in range(len(block) - charge_period + 1):
                chunk = block[i:i + charge_period]
                avg = sum(p['value'] for p in chunk) / charge_period
                average_charge_prices.append({'value': avg, 'periods': chunk})
        return average_charge_prices

    def get_lowest_average_charge_period(self, aapp: List[Dict[str, Any]], charge_period: int) -> Optional[Dict[str, Any]]:
        average_charge_prices = self.calc_average_charge_price(aapp, charge_period)
        if not average_charge_prices:
            return None
        average_charge_prices.sort(key=lambda x: x['value'])
        if self.logger.isEnabledFor(logging.DEBUG):
            self.print_average_charge_periods(average_charge_prices)
        return average_charge_prices[0]

    def _greedy_windows(
        self,
        available: List[Dict[str, Any]],
        total_periods: int,
        window_count: int,
        min_window_periods: int,
    ) -> Optional[List[Dict[str, Any]]]:
        remaining = list(available)
        selected: List[Dict[str, Any]] = []
        remaining_periods = total_periods
        remaining_windows = window_count

        while remaining_periods > 0 and remaining and remaining_windows > 0:
            size = math.ceil(remaining_periods / remaining_windows)
            size = min(max(size, min_window_periods), remaining_periods)

            best = self.get_lowest_average_charge_period(remaining, size)
            while best is None and size > 1:
                size -= 1
                best = self.get_lowest_average_charge_period(remaining, size)
            if best is None:
                return None

            selected.append(best)
            used = {(p['start'], p['end']) for p in best['periods']}
            remaining = [p for p in remaining if (p['start'], p['end']) not in used]
            remaining_periods -= len(best['periods'])
            remaining_windows -= 1

        if remaining_periods > 0:
            return None
        return selected

    def _to_windows(self, plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        windows = [
            {
                "start": block['periods'][0]['start'],
                "stop": block['periods'][-1]['end'],
                "avg": block['value'],
                "period_count": len(block['periods']),
            }
            for block in plan
        ]
        windows.sort(key=lambda x: x["start"])

        # Greedy selection can pick neighbouring blocks; present them as one session.
        merged: List[Dict[str, Any]] = []
        for window in windows:
            if merged and merged[-1]["stop"] == window["start"]:
                previous = merged[-1]
                total = previous["period_count"] + window["period_count"]
                previous["avg"] = (
                    previous["avg"] * previous["period_count"] + window["avg"] * window["period_count"]
                ) / total
                previous["period_count"] = total
                previous["stop"] = window["stop"]
            else:
                merged.append(dict(window))
        return merged

    def get_best_time_windows(
        self,
        total_periods: int,
        max_windows: int = 1,
        min_window_periods: int = 1,
        min_saving_ratio: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Pick the cheapest plan, only splitting into more sessions when it saves enough."""
        if total_periods <= 0 or not self.aapp:
            return []

        max_windows = max(1, int(max_windows))
        min_window_periods = max(1, int(min_window_periods))
        self.last_plan_stats = {}

        baseline_cost: Optional[float] = None
        best_plan: Optional[List[Dict[str, Any]]] = None
        best_cost: Optional[float] = None

        for window_count in range(1, max_windows + 1):
            if window_count > 1 and window_count * min_window_periods > total_periods:
                break

            plan = self._greedy_windows(self.aapp, total_periods, window_count, min_window_periods)
            if not plan:
                continue

            cost = sum(block['value'] * len(block['periods']) for block in plan)
            if baseline_cost is None:
                baseline_cost = best_cost = cost
                best_plan = plan
                continue

            if cost < best_cost and cost <= baseline_cost * (1 - min_saving_ratio):
                best_cost = cost
                best_plan = plan

        if not best_plan:
            return []

        windows = self._to_windows(best_plan)
        self.last_plan_stats = {
            "cost": round(best_cost, 4),
            "baseline_cost": round(baseline_cost, 4),
            "saving": round((baseline_cost - best_cost) / baseline_cost, 4) if baseline_cost else 0.0,
            "window_count": len(windows),
        }
        self.logger.debug(
            "Selected %s charging window(s), total cost %.4f (single-window baseline %.4f)",
            len(windows),
            best_cost,
            baseline_cost,
        )
        for window in windows:
            self.logger.debug(
                "Window start=%s stop=%s avg=%s periods=%s",
                window["start"],
                window["stop"],
                window["avg"],
                window["period_count"],
            )
        return windows

    def weighted_average(self, windows: List[Dict[str, Any]]) -> Optional[float]:
        total_periods = sum(window["period_count"] for window in windows)
        if total_periods <= 0:
            return None
        return sum(window["avg"] * window["period_count"] for window in windows) / total_periods

    def price_stats(self) -> Dict[str, Any]:
        if not self.aapp:
            return {"count": 0, "min": None, "max": None, "avg": None,
                    "min_at": None, "max_at": None, "horizon_end": None}
        cheapest = min(self.aapp, key=lambda p: p['value'])
        priciest = max(self.aapp, key=lambda p: p['value'])
        return {
            "count": len(self.aapp),
            "min": round(cheapest['value'], 4),
            "max": round(priciest['value'], 4),
            "avg": sum(p['value'] for p in self.aapp) / len(self.aapp),
            "min_at": cheapest['start'],
            "max_at": priciest['start'],
            "horizon_end": self.aapp[-1]['end'],
        }

    def expected_discharge_price(self, period_count: int) -> Optional[float]:
        """Average of the most expensive periods the stored energy could realistically replace."""
        if period_count <= 0 or not self.aapp:
            return None
        top = sorted((p['value'] for p in self.aapp), reverse=True)[:period_count]
        if not top:
            return None
        return sum(top) / len(top)

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

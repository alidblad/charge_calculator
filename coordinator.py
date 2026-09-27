"""Orchestrates planning, action execution and session persistence."""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CAR,
    DEFAULTS,
    DOMAIN,
    HOUSE,
    HOUSE_DISCHARGE,
    SIGNAL_PLAN_UPDATED,
    STATUS_CHARGING,
    STATUS_DISABLED,
    STATUS_DISCHARGING,
    STATUS_SCHEDULED,
    STATUS_UNAVAILABLE,
    STORAGE_KEY,
    STORAGE_VERSION,
    TASKS,
)
from .drain import DrainTracker
from .helpers import (
    as_float,
    config_get,
    local_hm,
    local_iso,
    render_service_data,
    to_timestamp,
)
from .prices import PriceHorizon
from .scheduler import weighted_average
from .solar import SolarForecast
from .strategies import CarStrategy, HouseDischargeStrategy, HouseStrategy

_LOGGER = logging.getLogger(__name__)

ACTIVE_STATUS = {HOUSE_DISCHARGE: STATUS_DISCHARGING}

LEGACY_SESSION_KEYS = {
    "label": "task",
    "charge_hours": "hours",
    "charge_power_kw": "power_kw",
    "stop_pct": "target_pct",
}


class ChargeCoordinator:
    """Single place that knows the current plan for every task."""

    def __init__(self, hass: HomeAssistant, cfg: Dict[str, Any]) -> None:
        self.hass = hass
        self.cfg = cfg or {}
        self.store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.enabled: Dict[str, bool] = {task: True for task in TASKS}
        self.plans: Dict[str, Dict[str, Any]] = {}
        self.prices: List[Dict[str, Any]] = []
        self.price_stats: Dict[str, Any] = {}

        house = self.cfg.get("house_battery", {}) or {}
        self.drain = DrainTracker(
            hass,
            soc_entity=house.get("sensor_id"),
            battery_size=as_float(house.get("size"), 0.0) or 0.0,
            load_entity=house.get("load_entity"),
            alpha=as_float(house.get("drain_alpha"), DEFAULTS["drain_alpha"]),
            min_samples=int(as_float(house.get("drain_min_samples"), DEFAULTS["drain_min_samples"])),
        )
        self.solar = SolarForecast(
            hass,
            weather_entity=self.cfg.get("weather_entity") or self.cfg.get("wether_entity"),
            peak_kw=as_float(self.cfg.get("solar_peak_kw"), 0.0) or 0.0,
            cloud_impact=as_float(
                self.cfg.get("solar_cloud_impact"), DEFAULTS["solar_cloud_impact"]
            ),
        )

        # Planning context, filled in per run.
        self.horizon: Optional[PriceHorizon] = None
        self.now: datetime.datetime = dt_util.utcnow()
        self._overrides: Dict[str, Any] = {}
        self._run_plans: Dict[str, Any] = {}

    # --- setup -------------------------------------------------------------

    async def async_load(self) -> None:
        stored = await self.store.async_load() or {}
        self.sessions = self._migrate_sessions(stored.get("sessions", {}) or {})
        stored_enabled = stored.get("enabled") or {}
        for task in TASKS:
            self.enabled[task] = bool(stored_enabled.get(task, True))
        await self.drain.async_load()

    def _migrate_sessions(self, stored: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Sessions saved before 0.4 used the old key names."""
        migrated: Dict[str, Dict[str, Any]] = {}
        for task, session in stored.items():
            if not isinstance(session, dict):
                continue
            converted = dict(session)
            for old, new in LEGACY_SESSION_KEYS.items():
                if old in converted and new not in converted:
                    converted[new] = converted.pop(old)
            converted["task"] = task
            converted.setdefault("session_index", 1)
            converted.setdefault("hours", 0)
            if converted.get("start_ts") is None or converted.get("stop_ts") is None:
                _LOGGER.warning("Discarding unusable stored session for %s", task)
                continue
            migrated[task] = converted
        return migrated

    async def async_save_sessions(self) -> None:
        await self.store.async_save({"sessions": self.sessions, "enabled": self.enabled})

    def enable_entity(self, task: str) -> Optional[str]:
        if task == HOUSE_DISCHARGE:
            return self.option("house_battery", "discharge_enable_entity")
        return self.option(f"{task}_battery", "enable_entity")

    def is_enabled(self, task: str) -> bool:
        if not self.enabled.get(task, True):
            return False
        entity = self.enable_entity(task)
        return self.state_is_on(entity) if entity else True

    async def async_set_enabled(self, task: str, value: bool) -> None:
        if self.enabled.get(task) == value:
            return
        self.enabled[task] = value
        _LOGGER.info("%s scheduling turned %s", task, "on" if value else "off")
        await self.async_save_sessions()
        await self.async_run(execute=True)

    # --- planning context API used by strategies ---------------------------

    def option(self, config_key: str, key: str, default: Any = None) -> Any:
        value = config_get(self.cfg, [config_key, key])
        return default if value is None else value

    def override(self, key: str, fallback: Any) -> Any:
        value = self._overrides.get(key)
        return fallback if value is None else value

    def state_float(self, entity_id: Optional[str]) -> Optional[float]:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Could not get state of %s", entity_id)
            return None
        value = as_float(state.state)
        if value is None:
            _LOGGER.error("Unable to parse state '%s' of %s", state.state, entity_id)
        return value

    def state_is_on(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and str(state.state).lower() in ("on", "true", "home", "connected")

    def scheduled_windows(self, task: str) -> List[Dict[str, Any]]:
        return self._run_plans.get(task, {}).get("windows", [])

    def plan_price(self, task: str) -> Optional[float]:
        windows = self.scheduled_windows(task)
        return weighted_average(windows) if windows else None

    # --- plan publishing ---------------------------------------------------

    @callback
    def publish(self, task: str, status: str, reason: str = "", **details: Any) -> None:
        plan = {
            "task": task,
            "status": status,
            "reason": reason,
            "updated_at": local_iso(dt_util.utcnow()),
            "sessions": [],
            "next_start": None,
            "next_stop": None,
            **details,
        }
        self.plans[task] = plan
        async_dispatcher_send(self.hass, SIGNAL_PLAN_UPDATED, task)

    def _describe(self, sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "index": item["session_index"],
                "start": local_iso(item["start_ts"]),
                "stop": local_iso(item["stop_ts"]),
                "hours": item["hours"],
                "avg_price": item.get("avg_price"),
            }
            for item in sessions
        ]

    # --- actions -----------------------------------------------------------

    def _action_key(self, task: str, kind: str) -> str:
        if task == HOUSE_DISCHARGE:
            return "house_discharge_action" if kind == "start" else "house_discharge_stop_action"
        return f"{task}_charge_action" if kind == "start" else f"{task}_charge_stop_action"

    def _context(self, session: Dict[str, Any]) -> Dict[str, Any]:
        power_kw = as_float(session.get("power_kw"), 0.0) or 0.0
        return {
            "label": session["task"],
            "task": session["task"],
            "session_index": session.get("session_index", 1),
            "start": local_iso(session["start_ts"]),
            "stop": local_iso(session["stop_ts"]),
            "start_ts": session["start_ts"],
            "stop_ts": session["stop_ts"],
            "charge_hours": session.get("hours", 0),
            "stop_pct": session.get("target_pct"),
            "target_pct": session.get("target_pct"),
            "reserve_pct": session.get("reserve_pct"),
            "current_pct": session.get("current_pct"),
            "charge_power_kw": power_kw,
            "charge_power_w": int(power_kw * 1000),
            "avg_price": session.get("avg_price"),
        }

    async def call_action(self, session: Dict[str, Any], kind: str) -> bool:
        task = session["task"]
        key = self._action_key(task, kind)
        action_cfg = self.cfg.get(key, {}) or {}
        service = action_cfg.get("service")

        if not service:
            _LOGGER.debug("No %s action configured for %s", kind, task)
            return False
        if not isinstance(service, str) or "." not in service:
            _LOGGER.error("Invalid service configured for %s: %s", key, service)
            return False

        data = action_cfg.get("data", {}) or {}
        if not isinstance(data, dict):
            _LOGGER.error("Configured action data for %s must be a dictionary", key)
            return False

        domain, service_name = service.split(".", 1)
        rendered = render_service_data(data, self._context(session))
        await self.hass.services.async_call(domain, service_name, rendered, blocking=True)
        _LOGGER.info(
            "ACTION %s %s: called %s (window %s -> %s, %.2f kW, avg price %s) data=%s",
            kind.upper(),
            task,
            service,
            local_hm(session["start_ts"]),
            local_hm(session["stop_ts"]),
            as_float(session.get("power_kw"), 0.0) or 0.0,
            session.get("avg_price"),
            rendered,
        )
        return True

    async def _force_stop(self, task: str, reason: str) -> None:
        active = self.sessions.get(task)
        if not active:
            return
        active["task"] = task
        _LOGGER.info("%s: %s, stopping the running session early", task, reason)
        await self.call_action(active, "stop")
        self.sessions.pop(task, None)
        await self.async_save_sessions()

    async def _reconcile(self, task: str, now_ts: float) -> Optional[Dict[str, Any]]:
        active = self.sessions.get(task)
        if not active:
            return None

        if now_ts >= active["stop_ts"]:
            _LOGGER.info(
                "%s session finished (%s -> %s), sending stop",
                task,
                local_hm(active["start_ts"]),
                local_hm(active["stop_ts"]),
            )
            await self.call_action(active, "stop")
            self.sessions.pop(task, None)
            await self.async_save_sessions()
            return None

        if now_ts < active["start_ts"]:
            self.sessions.pop(task, None)
            await self.async_save_sessions()
            return None

        active["task"] = task
        return active

    async def _start_if_due(self, sessions: List[Dict[str, Any]], now_ts: float) -> Optional[Dict[str, Any]]:
        due = next(
            (item for item in sessions if item["start_ts"] <= now_ts < item["stop_ts"]), None
        )
        if due is None:
            return None

        task = due["task"]
        if self.sessions.get(task, {}).get("start_ts") == due["start_ts"]:
            return self.sessions[task]

        if await self.call_action(due, "start"):
            self.sessions[task] = due
            await self.async_save_sessions()
            return due
        return None

    # --- main entry point --------------------------------------------------

    async def async_run(self, overrides: Optional[Dict[str, Any]] = None, execute: bool = False) -> Dict[str, Any]:
        self._overrides = overrides or {}
        self._run_plans = {}
        self.now = dt_util.utcnow()
        now_ts = dt_util.as_timestamp(self.now)

        _LOGGER.info("--- Charge calculator run (execute=%s) ---", execute)

        nordpol_entity = self.cfg.get("nordpol_entity")
        nordpol_state = self.hass.states.get(nordpol_entity) if nordpol_entity else None
        if nordpol_state is None:
            _LOGGER.error("Price entity %s is unavailable, aborting", nordpol_entity)
            for task in TASKS:
                self.publish(task, STATUS_UNAVAILABLE, f"Price entity {nordpol_entity} unavailable")
            return {}

        self.horizon = PriceHorizon(nordpol_state, self.now)
        if not self.horizon:
            _LOGGER.error("No usable price periods, aborting")
            for task in TASKS:
                self.publish(task, STATUS_UNAVAILABLE, "No usable price periods")
            return {}

        await self.solar.async_refresh()
        await self.drain.async_save()

        self.price_stats = self.horizon.stats()
        self.prices = self.horizon.as_attribute_list()
        self._log_horizon()

        result: Dict[str, Any] = {}
        for strategy_cls in (CarStrategy, HouseStrategy, HouseDischargeStrategy):
            strategy = strategy_cls(self)
            task = strategy.task

            if not self.is_enabled(task):
                await self._force_stop(task, "scheduling is turned off")
                entity = self.enable_entity(task)
                reason = (
                    f"Turned off by {entity}"
                    if entity and not self.state_is_on(entity)
                    else "Turned off"
                )
                _LOGGER.info("%s: %s", task, reason)
                self.publish(task, STATUS_DISABLED, reason)
                continue

            active = await self._reconcile(task, now_ts) if execute else None
            if active is not None:
                # Keep the running window visible so the discharge strategy will not overlap it.
                self._run_plans[task] = {
                    "windows": [
                        {
                            "start": dt_util.utc_from_timestamp(active["start_ts"]),
                            "stop": dt_util.utc_from_timestamp(active["stop_ts"]),
                            "avg": active.get("avg_price") or 0.0,
                            "period_count": 1,
                        }
                    ]
                }
                self._publish_active(active, now_ts, "Session in progress, plan pinned until it ends")
                result[task] = [active]
                continue

            outcome = strategy.plan()
            self._run_plans[task] = {"windows": outcome.windows}

            if not outcome.windows:
                _LOGGER.info("%s: %s (%s)", task, outcome.status, outcome.reason)
                self.publish(task, outcome.status, outcome.reason, **self._common(outcome.details))
                continue

            sessions = self._build_sessions(outcome)
            self._log_sessions(task, sessions, outcome.details)
            self.publish(
                task,
                STATUS_SCHEDULED,
                outcome.reason,
                sessions=self._describe(sessions),
                session_count=len(sessions),
                next_start=local_iso(sessions[0]["start_ts"]),
                next_stop=local_iso(sessions[0]["stop_ts"]),
                average_price=round(weighted_average(outcome.windows) or 0, 4),
                **self._common(outcome.details),
            )
            result[task] = sessions

            if execute:
                started = await self._start_if_due(sessions, now_ts)
                if started:
                    self._publish_active(started, now_ts, "Started for this window")

        return result

    # --- helpers -----------------------------------------------------------

    def _common(self, details: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(details)
        merged.update(
            {
                "price_min": self.price_stats.get("min"),
                "price_max": self.price_stats.get("max"),
                "price_avg": round(self.price_stats["avg"], 4)
                if self.price_stats.get("avg") is not None
                else None,
                "horizon_end": local_iso(self.price_stats.get("horizon_end")),
                "tomorrow_prices_available": self.horizon.tomorrow_valid if self.horizon else False,
            }
        )
        return merged

    def _build_sessions(self, outcome) -> List[Dict[str, Any]]:
        details = outcome.details
        power_kw = details.get("charge_power_kw") or details.get("discharge_power_kw")
        sessions: List[Dict[str, Any]] = []
        for index, window in enumerate(outcome.windows, start=1):
            start_ts = to_timestamp(window["start"])
            stop_ts = to_timestamp(window["stop"])
            if start_ts is None or stop_ts is None:
                continue
            sessions.append(
                {
                    "task": outcome.task,
                    "session_index": index,
                    "start_ts": start_ts,
                    "stop_ts": stop_ts,
                    "hours": round(window["period_count"] * self.horizon.period_hours, 3),
                    "avg_price": round(window["avg"], 4),
                    "power_kw": power_kw,
                    "target_pct": details.get("target_pct"),
                    "reserve_pct": details.get("reserve_pct"),
                    "current_pct": details.get("current_pct"),
                }
            )
        return sessions

    @callback
    def _publish_active(self, session: Dict[str, Any], now_ts: float, reason: str) -> None:
        task = session["task"]
        self.publish(
            task,
            ACTIVE_STATUS.get(task, STATUS_CHARGING),
            reason,
            sessions=self._describe([session]),
            session_count=1,
            next_start=local_iso(session["start_ts"]),
            next_stop=local_iso(session["stop_ts"]),
            current_pct=session.get("current_pct"),
            target_pct=session.get("target_pct"),
            charge_power_kw=session.get("power_kw"),
            average_price=session.get("avg_price"),
            minutes_remaining=round((session["stop_ts"] - now_ts) / 60),
        )

    def _log_horizon(self) -> None:
        stats = self.price_stats
        _LOGGER.info(
            "Prices: %s periods of %s min until %s (tomorrow_valid=%s) | min %.4f @ %s, max %.4f @ %s, avg %.4f",
            stats["count"],
            self.horizon.period_minutes,
            local_hm(stats["horizon_end"]),
            self.horizon.tomorrow_valid,
            stats["min"],
            local_hm(stats["min_at"]),
            stats["max"],
            local_hm(stats["max_at"]),
            stats["avg"],
        )
        drain = self.drain.diagnostics()
        if drain["current_rate_kw"] is not None:
            _LOGGER.info(
                "House drain: %.3f kW now, %s of 48 hour buckets learned (from %s)",
                drain["current_rate_kw"],
                drain["learned_hours"],
                drain["source"],
            )
        if self.solar.available:
            solar = self.solar.diagnostics(self.now)
            _LOGGER.info(
                "Solar forecast: %.2f kWh left today, %.2f kWh tomorrow (cloud %.0f%%)",
                solar["rest_of_today_kwh"],
                solar["tomorrow_kwh"],
                solar["cloud_now"],
            )

    def _log_sessions(self, task: str, sessions: List[Dict[str, Any]], details: Dict[str, Any]) -> None:
        if details.get("hours_needed") is not None:
            _LOGGER.info(
                "%s: soc=%s%% target=%s%% -> %.2f h (%s periods) at %s kW",
                task,
                details.get("current_pct"),
                details.get("target_pct"),
                details["hours_needed"],
                details.get("periods_needed"),
                details.get("charge_power_kw") or details.get("discharge_power_kw"),
            )
        if details.get("solar_offset_kwh"):
            _LOGGER.info(
                "%s: solar is expected to contribute %.2f kWh, buying that much less",
                task,
                details["solar_offset_kwh"],
            )
        if details.get("hours_until_reserve") is not None:
            _LOGGER.info(
                "%s: battery reaches its reserve in %.1f h (%s)",
                task,
                details["hours_until_reserve"],
                details.get("empty_at"),
            )
        for item in sessions:
            _LOGGER.info(
                "%s: session %s/%s %s -> %s (%.2f h, avg %.4f)",
                task,
                item["session_index"],
                len(sessions),
                local_hm(item["start_ts"]),
                local_hm(item["stop_ts"]),
                item["hours"],
                item["avg_price"],
            )
        if details.get("saving_vs_single_window"):
            _LOGGER.info(
                "%s: %s session(s) cost %.4f vs %.4f for one continuous window (saving %.1f%%)",
                task,
                len(sessions),
                details["plan_cost"],
                details["baseline_cost"],
                details["saving_vs_single_window"] * 100,
            )

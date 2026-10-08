"""Per-battery planning strategies.

Car and house battery share the price model but optimise for different things:
the car is a deadline-constrained cost minimisation, the house battery is arbitrage.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from homeassistant.util import dt as dt_util

from . import scheduler
from .const import (
    CAR,
    CAR_DEFAULTS,
    DEFAULTS,
    HOUSE,
    HOUSE_DEFAULTS,
    HOUSE_DISCHARGE,
    STATUS_BLOCKED,
    STATUS_IDLE,
    STATUS_NOT_PROFITABLE,
    STATUS_NO_WINDOW,
    STATUS_SCHEDULED,
)
from .helpers import as_float, as_int, local_hm, next_occurrence, parse_time_of_day

_LOGGER = logging.getLogger(__name__)


class PlanResult:
    """Outcome of one planning pass for one task."""

    def __init__(
        self,
        task: str,
        status: str,
        reason: str = "",
        windows: Optional[List[Dict[str, Any]]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.task = task
        self.status = status
        self.reason = reason
        self.windows = windows or []
        self.details = details or {}


class BaseStrategy:
    task = ""
    config_key = ""
    power_key = "charge_effect"

    def __init__(self, context) -> None:
        self.ctx = context

    def option(self, key: str, default: Any = None) -> Any:
        return self.ctx.option(self.config_key, key, default)

    @property
    def horizon(self):
        return self.ctx.horizon

    @property
    def now(self) -> datetime.datetime:
        return self.ctx.now

    def _window_constraints(self) -> Dict[str, int]:
        return {
            "min_window_periods": self.horizon.periods_for_minutes(
                self.option("min_session_minutes", DEFAULTS["min_session_minutes"])
            ),
            "min_saving_ratio": as_float(
                self.option("min_saving_ratio", DEFAULTS["min_saving_ratio"]),
                DEFAULTS["min_saving_ratio"],
            ),
        }

    def _charge_hours(self, current_pct: float, target_pct: float, power_kw: float) -> float:
        size = as_float(self.option("size"), 0.0) or 0.0
        missing = ((target_pct - current_pct) / 100.0) * size
        if missing <= 0 or power_kw <= 0:
            return 0.0
        hours = missing / power_kw
        minimum = as_float(self.option("min_charge_time"), 0.0) or 0.0
        return max(hours, minimum)


class CarStrategy(BaseStrategy):
    """Reach the target state of charge as cheaply as possible before departure."""

    task = CAR
    config_key = "car_battery"

    def plan(self) -> PlanResult:
        power_kw = as_float(
            self.ctx.override("car_charge_effect", self.option("charge_effect")),
            CAR_DEFAULTS["charge_effect"],
        )
        target_pct = as_int(
            self.ctx.override("car_charge_stop", self.option("charge_stop")),
            CAR_DEFAULTS["charge_stop"],
        )
        current_pct = self.ctx.state_float(self.option("sensor_id"))

        details = {
            "current_pct": current_pct,
            "target_pct": target_pct,
            "charge_power_kw": power_kw,
        }

        if current_pct is None:
            return PlanResult(self.task, STATUS_IDLE, "No state-of-charge reading", details=details)

        plug_entity = self.option("plugged_in_entity")
        if plug_entity and not self.ctx.state_is_on(plug_entity):
            return PlanResult(
                self.task, STATUS_BLOCKED, "Car is not plugged in", details=details
            )

        hours = self._charge_hours(current_pct, target_pct, power_kw)
        if hours <= 0:
            return PlanResult(
                self.task, STATUS_IDLE, "Target state of charge already reached", details=details
            )

        deadline = None
        ready_by_entity = self.option("ready_by_entity")
        if ready_by_entity:
            state = self.ctx.hass.states.get(ready_by_entity)
            ready_by = parse_time_of_day(state.state) if state else None
        else:
            ready_by = parse_time_of_day(self.option("ready_by"))
        if ready_by is not None:
            deadline = next_occurrence(ready_by, self.now)
            details["ready_by"] = local_hm(deadline)

        periods = self.horizon.before(deadline)
        needed = self.horizon.periods_for_hours(hours)
        details.update({"hours_needed": round(hours, 2), "periods_needed": needed})

        if deadline is not None and len(periods) < needed:
            _LOGGER.info(
                "%s: only %s periods before the %s deadline, need %s - ignoring the deadline",
                self.task,
                len(periods),
                local_hm(deadline),
                needed,
            )
            periods = self.horizon.periods
            details["deadline_relaxed"] = True

        constraints = self._window_constraints()
        plan = scheduler.plan_windows(
            periods,
            total_periods=needed,
            max_windows=as_int(
                self.ctx.override("car_max_sessions", self.option("max_sessions")),
                CAR_DEFAULTS["max_sessions"],
            ),
            **constraints,
        )
        details.update(
            {
                "plan_cost": plan["cost"],
                "baseline_cost": plan["baseline_cost"],
                "saving_vs_single_window": plan["saving"],
            }
        )

        if not plan["windows"]:
            return PlanResult(
                self.task, STATUS_NO_WINDOW, "No window fits in the price horizon", details=details
            )

        return PlanResult(
            self.task,
            STATUS_SCHEDULED,
            "Cheapest window(s) before the deadline",
            windows=plan["windows"],
            details=details,
        )


class HouseStrategy(BaseStrategy):
    """Buy only what is needed to bridge to the next cheap spot, and only when it pays."""

    task = HOUSE
    config_key = "house_battery"

    def plan(self) -> PlanResult:
        power_kw = as_float(
            self.ctx.override("house_charge_effect", self.option("charge_effect")),
            HOUSE_DEFAULTS["charge_effect"],
        )
        target_pct = as_int(
            self.ctx.override("house_charge_stop", self.option("charge_stop")),
            HOUSE_DEFAULTS["charge_stop"],
        )
        reserve_pct = as_float(self.option("reserve_pct"), HOUSE_DEFAULTS["reserve_pct"])
        size = as_float(self.option("size"), 0.0) or 0.0
        current_pct = self.ctx.state_float(self.option("sensor_id"))

        details = {
            "current_pct": current_pct,
            "target_pct": target_pct,
            "reserve_pct": reserve_pct,
            "charge_power_kw": power_kw,
        }

        if current_pct is None:
            return PlanResult(self.task, STATUS_IDLE, "No state-of-charge reading", details=details)

        runway_hours = self.ctx.drain.hours_until(current_pct, reserve_pct, self.now)
        if runway_hours is not None:
            details["hours_until_reserve"] = round(runway_hours, 1)
            details["empty_at"] = local_hm(self.now + datetime.timedelta(hours=runway_hours))

        solar_kwh = self._usable_solar(current_pct, size, target_pct, runway_hours)
        if solar_kwh:
            details["solar_offset_kwh"] = round(solar_kwh, 2)

        missing_kwh = max(((target_pct - current_pct) / 100.0) * size - solar_kwh, 0.0)
        details["energy_needed_kwh"] = round(missing_kwh, 2)

        if missing_kwh <= 0:
            reason = (
                "Solar is expected to cover the remaining capacity"
                if solar_kwh
                else "Target state of charge already reached"
            )
            return PlanResult(self.task, STATUS_IDLE, reason, details=details)

        hours = max(
            missing_kwh / power_kw if power_kw > 0 else 0.0,
            as_float(self.option("min_charge_time"), 0.0) or 0.0,
        )
        if hours <= 0:
            return PlanResult(self.task, STATUS_IDLE, "Nothing to charge", details=details)

        deadline = None
        if runway_hours is not None:
            deadline = self.now + datetime.timedelta(hours=runway_hours)

        periods = self.horizon.before(deadline)
        needed = self.horizon.periods_for_hours(hours)
        details.update({"hours_needed": round(hours, 2), "periods_needed": needed})

        urgent_fallback = False
        if deadline is not None and len(periods) < needed:
            details["deadline_relaxed"] = True
            periods = scheduler.earliest_contiguous_periods(self.horizon.periods, needed)
            urgent_fallback = bool(periods)
            if not periods:
                periods = self.horizon.periods

        constraints = self._window_constraints()
        plan = scheduler.plan_windows(
            periods,
            total_periods=needed,
            max_windows=as_int(
                self.ctx.override("house_max_sessions", self.option("max_sessions")),
                HOUSE_DEFAULTS["max_sessions"],
            ),
            **constraints,
        )
        details.update(
            {
                "plan_cost": plan["cost"],
                "baseline_cost": plan["baseline_cost"],
                "saving_vs_single_window": plan["saving"],
            }
        )

        if not plan["windows"]:
            return PlanResult(
                self.task, STATUS_NO_WINDOW, "No window fits in the price horizon", details=details
            )

        if self.option("break_even", False):
            verdict = self._break_even(plan["windows"], needed, details)
            if verdict is not None:
                return verdict

        return PlanResult(
            self.task,
            STATUS_SCHEDULED,
            (
                "Earliest available window; reserve deadline cannot fit a full session"
                if urgent_fallback
                else "Cheapest available window; reserve deadline cannot fit a full session"
                if details.get("deadline_relaxed")
                else "Cheapest window(s) before the battery reaches its reserve"
            ),
            windows=plan["windows"],
            details=details,
        )

    def _usable_solar(
        self,
        current_pct: float,
        size: float,
        target_pct: int,
        runway_hours: Optional[float],
    ) -> float:
        """PV surplus (production minus house load) expected before the battery runs down."""
        if not self.ctx.solar.available:
            return 0.0

        window_hours = min(runway_hours, 24.0) if runway_hours is not None else 18.0
        end = self.now + datetime.timedelta(hours=window_hours)
        production = self.ctx.solar.expected_kwh(self.now, end)
        if production <= 0:
            return 0.0

        consumption = self.ctx.drain.predict_kwh(self.now, end) or 0.0
        surplus = max(production - consumption, 0.0)
        headroom = max(((target_pct - current_pct) / 100.0) * size, 0.0)
        return min(surplus, headroom)

    def _break_even(
        self, windows: List[Dict[str, Any]], needed: int, details: Dict[str, Any]
    ) -> Optional[PlanResult]:
        efficiency = as_float(
            self.option("round_trip_efficiency", DEFAULTS["round_trip_efficiency"]),
            DEFAULTS["round_trip_efficiency"],
        )
        cycle_cost = as_float(
            self.option("cycle_cost", DEFAULTS["cycle_cost"]), DEFAULTS["cycle_cost"]
        )
        charge_price = scheduler.weighted_average(windows)
        discharge_price = self.horizon.expected_discharge_price(needed)
        if charge_price is None or discharge_price is None or efficiency <= 0:
            return None

        effective = charge_price / efficiency + cycle_cost
        details["effective_charge_price"] = round(effective, 4)
        details["expected_discharge_price"] = round(discharge_price, 4)
        if effective >= discharge_price:
            return PlanResult(
                self.task,
                STATUS_NOT_PROFITABLE,
                f"Not profitable: {charge_price:.4f} / {efficiency:.2f} + {cycle_cost:.4f} "
                f"= {effective:.4f} >= {discharge_price:.4f}",
                details=details,
            )
        return None


class HouseDischargeStrategy(BaseStrategy):
    """Push stored energy into the most expensive hours, if the spread justifies it."""

    task = HOUSE_DISCHARGE
    config_key = "house_battery"

    def plan(self) -> PlanResult:
        if not self.option("discharge", False):
            return PlanResult(self.task, STATUS_IDLE, "Discharge scheduling is disabled")

        power_kw = as_float(
            self.option("discharge_effect"), HOUSE_DEFAULTS["discharge_effect"]
        )
        reserve_pct = as_float(self.option("reserve_pct"), HOUSE_DEFAULTS["reserve_pct"])
        size = as_float(self.option("size"), 0.0) or 0.0
        current_pct = self.ctx.state_float(self.option("sensor_id"))

        details = {
            "current_pct": current_pct,
            "reserve_pct": reserve_pct,
            "discharge_power_kw": power_kw,
        }

        if current_pct is None or size <= 0 or power_kw <= 0:
            return PlanResult(self.task, STATUS_IDLE, "Discharge not possible", details=details)

        available_kwh = ((current_pct - reserve_pct) / 100.0) * size
        details["available_kwh"] = round(available_kwh, 2)
        if available_kwh <= 0:
            return PlanResult(
                self.task, STATUS_IDLE, "Battery is at its reserve", details=details
            )

        needed = self.horizon.periods_for_hours(available_kwh / power_kw)
        min_periods = self.horizon.periods_for_minutes(
            self.option("min_session_minutes", DEFAULTS["min_session_minutes"])
        )
        needed = max(needed, min_periods)

        # Never discharge into a window we have scheduled for charging.
        charge_windows = self.ctx.scheduled_windows(HOUSE)
        periods = [
            period
            for period in self.horizon.periods
            if not any(
                period["start"] < window["stop"] and period["end"] > window["start"]
                for window in charge_windows
            )
        ]

        best = scheduler.priciest_window(periods, needed)
        while best is None and needed > min_periods:
            needed -= 1
            best = scheduler.priciest_window(periods, needed)
        if best is None:
            return PlanResult(
                self.task, STATUS_NO_WINDOW, "No discharge window available", details=details
            )

        window = {
            "start": best["periods"][0]["start"],
            "stop": best["periods"][-1]["end"],
            "avg": best["value"],
            "period_count": len(best["periods"]),
        }

        charge_price = self.ctx.plan_price(HOUSE)
        if charge_price is None:
            charge_price = self.horizon.stats().get("min")
        min_spread = as_float(
            self.option("min_discharge_spread"), HOUSE_DEFAULTS["min_discharge_spread"]
        )
        efficiency = as_float(
            self.option("round_trip_efficiency", DEFAULTS["round_trip_efficiency"]),
            DEFAULTS["round_trip_efficiency"],
        )
        cycle_cost = as_float(
            self.option("cycle_cost", DEFAULTS["cycle_cost"]), DEFAULTS["cycle_cost"]
        )

        spread = window["avg"] - ((charge_price or 0.0) / max(efficiency, 0.01) + cycle_cost)
        details.update(
            {
                "discharge_price": round(window["avg"], 4),
                "reference_charge_price": round(charge_price, 4) if charge_price else None,
                "spread": round(spread, 4),
                "min_spread": min_spread,
            }
        )

        if spread < min_spread:
            return PlanResult(
                self.task,
                STATUS_NOT_PROFITABLE,
                f"Spread {spread:.4f} is below the {min_spread:.4f} threshold",
                details=details,
            )

        return PlanResult(
            self.task,
            STATUS_SCHEDULED,
            "Discharging into the most expensive hours",
            windows=[window],
            details=details,
        )


STRATEGIES = (CarStrategy, HouseStrategy, HouseDischargeStrategy)

"""Constants for the Charge Calculator integration."""

DOMAIN = "charge_calculator"

STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 1
DRAIN_STORAGE_KEY = f"{DOMAIN}.drain"
DRAIN_STORAGE_VERSION = 1

SIGNAL_PLAN_UPDATED = f"{DOMAIN}_plan_updated"

CAR = "car"
HOUSE = "house"
HOUSE_DISCHARGE = "house_discharge"

TASKS = (CAR, HOUSE, HOUSE_DISCHARGE)
LABEL_NAMES = {
    CAR: "car",
    HOUSE: "house battery",
    HOUSE_DISCHARGE: "house battery discharge",
}

STATUS_CHARGING = "charging"
STATUS_DISCHARGING = "discharging"
STATUS_SCHEDULED = "scheduled"
STATUS_IDLE = "idle"
STATUS_NO_WINDOW = "no_window"
STATUS_NOT_PROFITABLE = "not_profitable"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BLOCKED = "blocked"
STATUS_DISABLED = "disabled"

DAY_TYPES = ("weekday", "weekend")

DEFAULTS = {
    "interval_minutes": 5,
    "event_throttle_seconds": 60,
    "min_session_minutes": 60,
    "min_saving_ratio": 0.03,
    "round_trip_efficiency": 0.9,
    "cycle_cost": 0.0,
    "drain_alpha": 0.2,
    "drain_min_samples": 3,
    "solar_cloud_impact": 0.75,
}

CAR_DEFAULTS = {
    "charge_effect": 6.6,
    "charge_stop": 80,
    "max_sessions": 1,
}

HOUSE_DEFAULTS = {
    "charge_effect": 4.0,
    "charge_stop": 90,
    "max_sessions": 1,
    "reserve_pct": 10,
    "discharge_effect": 4.0,
    "min_discharge_spread": 0.20,
}

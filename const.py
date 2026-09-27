DOMAIN = "charge_calculator"

STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 1

SIGNAL_PLAN_UPDATED = f"{DOMAIN}_plan_updated"

LABELS = ("car", "house")
LABEL_NAMES = {"car": "car", "house": "house battery"}

STATUS_CHARGING = "charging"
STATUS_SCHEDULED = "scheduled"
STATUS_IDLE = "idle"
STATUS_NO_WINDOW = "no_window"
STATUS_NOT_PROFITABLE = "not_profitable"
STATUS_UNAVAILABLE = "unavailable"

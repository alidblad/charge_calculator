"""Focused planner tests with only Home Assistant adapters stubbed."""
import datetime as dt
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


def load_modules():
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("_charge_calculator_test")
    package.__path__ = [str(root)]
    dates = types.ModuleType("homeassistant.util.dt")
    dates.as_local = lambda value: value
    dates.utcnow = lambda: dt.datetime.now(dt.timezone.utc)
    dates.parse_datetime = dt.datetime.fromisoformat
    modules = {"_charge_calculator_test": package}
    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.event", "homeassistant.helpers.storage",
        "homeassistant.util",
    ):
        modules[name] = types.ModuleType(name)
    modules["homeassistant.core"].HomeAssistant = Mock
    modules["homeassistant.core"].callback = lambda func: func
    modules["homeassistant.helpers.event"].async_track_state_change_event = Mock()
    modules["homeassistant.helpers.storage"].Store = Mock()
    modules["homeassistant.util"].dt = dates
    modules["homeassistant.util.dt"] = dates
    with patch.dict(sys.modules, modules):
        return tuple(
            importlib.import_module(f"_charge_calculator_test.{name}")
            for name in ("scheduler", "prices", "strategies", "drain")
        )


scheduler, prices, strategies, drain = load_modules()
START = dt.datetime(2026, 10, 15, 19, 30, tzinfo=dt.timezone.utc)


def periods(values, start=START):
    return [
        {
            "start": start + dt.timedelta(minutes=15 * index),
            "end": start + dt.timedelta(minutes=15 * (index + 1)),
            "value": value,
        }
        for index, value in enumerate(values)
    ]


class Context:
    def __init__(self, values, runway, max_sessions=1):
        self.now = START
        self.horizon = prices.PriceHorizon(
            types.SimpleNamespace(attributes={"raw_today": periods(values)}), START
        )
        self.drain = types.SimpleNamespace(hours_until=lambda *args: runway)
        self.solar = types.SimpleNamespace(available=False)
        self.options = {
            "size": 10, "charge_effect": 4, "charge_stop": 90,
            "sensor_id": "sensor.soc", "max_sessions": max_sessions,
        }

    def option(self, group, key, default=None):
        return self.options.get(key, default)

    def override(self, key, fallback):
        return fallback

    def state_float(self, entity):
        return 50


class HousePlanningTests(unittest.TestCase):
    def test_session_can_finish_after_reserve(self):
        result = strategies.HouseStrategy(Context([1] * 6 + [.1] * 8, 1.5)).plan()
        window = result.windows[0]
        self.assertEqual(window["start"], START + dt.timedelta(hours=1.5))
        self.assertEqual(window["stop"], START + dt.timedelta(hours=2.5))
        self.assertNotIn("deadline_relaxed", result.details)

    def test_short_runway_does_not_force_earliest_window(self):
        result = strategies.HouseStrategy(Context([2] + [.1] * 4, .25)).plan()
        self.assertEqual(result.windows[0]["start"], START + dt.timedelta(minutes=15))
        self.assertNotIn("deadline_relaxed", result.details)

    def test_no_runway_chooses_cheapest_in_entire_horizon(self):
        result = strategies.HouseStrategy(Context([2] * 8 + [.1] * 4, None)).plan()
        self.assertEqual(result.windows[0]["start"], START + dt.timedelta(hours=2))

    def test_zero_runway_starts_now(self):
        result = strategies.HouseStrategy(Context([2] * 4 + [.1] * 4, 0)).plan()
        self.assertEqual(result.windows[0]["start"], START)
        self.assertNotIn("deadline_relaxed", result.details)

    def test_unreachable_start_deadline_uses_earliest_full_window(self):
        ctx = Context([2] * 4 + [.1] * 4, .25)
        ctx.horizon.periods = periods(
            [2] * 4 + [.1] * 4, start=START + dt.timedelta(hours=1)
        )
        result = strategies.HouseStrategy(ctx).plan()
        self.assertTrue(result.details["deadline_relaxed"])
        self.assertEqual(result.windows[0]["start"], START + dt.timedelta(hours=1))

    def test_insufficient_prices_returns_no_window(self):
        result = strategies.HouseStrategy(Context([1, 1], .25)).plan()
        self.assertEqual(result.status, "no_window")
        self.assertEqual(result.windows, [])

    def test_split_sessions_respect_start_deadline(self):
        data = periods([.1] * 4 + [2] * 4 + [.1] * 4 + [2] * 4 + [.01] * 4)
        deadline = START + dt.timedelta(hours=2)
        result = scheduler.plan_windows(
            data, 8, max_windows=2, min_window_periods=4,
            min_saving_ratio=.05, latest_start=deadline,
        )
        self.assertEqual(len(result["windows"]), 2)
        self.assertEqual(sum(w["period_count"] for w in result["windows"]), 8)
        self.assertTrue(all(w["start"] <= deadline for w in result["windows"]))
        self.assertEqual(result["windows"][-1]["stop"], START + dt.timedelta(hours=3))

    def test_unconstrained_scheduler_behavior_unchanged(self):
        result = scheduler.plan_windows(periods([1] * 4 + [.1] * 4), 4)
        self.assertEqual(result["windows"][0]["start"], START + dt.timedelta(hours=1))

    def test_start_constraint_does_not_bridge_gaps(self):
        data = periods([.1, .1]) + periods([.1, .1], START + dt.timedelta(hours=1))
        result = scheduler.plan_windows(data, 4, latest_start=START)
        self.assertEqual(result["windows"], [])


class DrainSourceTests(unittest.IsolatedAsyncioTestCase):
    def tracker(self, stored, load_entity="sensor.house_without_ev"):
        tracker = drain.DrainTracker(
            Mock(), "sensor.soc", 10, load_entity=load_entity
        )
        tracker._store = types.SimpleNamespace(
            async_load=AsyncMock(return_value=stored), async_save=AsyncMock()
        )
        return tracker

    def stored(self, source):
        return {
            "load_entity": source,
            "profile": {key: [8.0] * 24 for key in ("weekday", "weekend")},
            "counts": {key: [3] * 24 for key in ("weekday", "weekend")},
        }

    async def test_changed_source_clears_and_persists_profile(self):
        tracker = self.tracker(self.stored("sensor.old_load"))
        await tracker.async_load()
        self.assertEqual(tracker.learned_hours, 0)
        await tracker.async_save()
        saved = tracker._store.async_save.call_args.args[0]
        self.assertEqual(saved["load_entity"], "sensor.house_without_ev")
        self.assertEqual(saved["profile"]["weekday"], [None] * 24)
        self.assertEqual(saved["counts"]["weekday"], [0] * 24)

    async def test_unchanged_source_preserves_profile(self):
        tracker = self.tracker(self.stored("sensor.house_without_ev"))
        await tracker.async_load()
        self.assertEqual(tracker.learned_hours, 48)
        await tracker.async_save()
        tracker._store.async_save.assert_not_awaited()

    async def test_legacy_load_profile_is_relearned_once(self):
        stored = self.stored("sensor.old_load")
        del stored["load_entity"]
        tracker = self.tracker(stored)
        await tracker.async_load()
        self.assertEqual(tracker.learned_hours, 0)
        await tracker.async_save()
        saved = tracker._store.async_save.call_args.args[0]
        reloaded = self.tracker(saved)
        await reloaded.async_load()
        await reloaded.async_save()
        reloaded._store.async_save.assert_not_awaited()

    async def test_legacy_soc_profile_is_preserved(self):
        stored = self.stored(None)
        del stored["load_entity"]
        tracker = self.tracker(stored, load_entity=None)
        await tracker.async_load()
        self.assertEqual(tracker.learned_hours, 48)


if __name__ == "__main__":
    unittest.main()

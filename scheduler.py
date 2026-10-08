"""Window selection: find the cheapest (or priciest) contiguous periods to act in."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)


def contiguous_blocks(periods: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split a time-sorted period list into runs with no gaps."""
    blocks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for period in sorted(periods, key=lambda item: item["start"]):
        if current and period["start"] != current[-1]["end"]:
            blocks.append(current)
            current = []
        current.append(period)
    if current:
        blocks.append(current)
    return blocks


def earliest_contiguous_periods(
    periods: List[Dict[str, Any]], size: int
) -> List[Dict[str, Any]]:
    """Return the first contiguous block large enough for the requested session."""
    if size <= 0:
        return []
    for block in contiguous_blocks(periods):
        if len(block) >= size:
            return block[:size]
    return []


def candidate_windows(periods: List[Dict[str, Any]], size: int) -> List[Dict[str, Any]]:
    """Every contiguous run of `size` periods, with its average price."""
    candidates: List[Dict[str, Any]] = []
    if size <= 0:
        return candidates
    for block in contiguous_blocks(periods):
        for index in range(len(block) - size + 1):
            chunk = block[index : index + size]
            candidates.append(
                {"value": sum(p["value"] for p in chunk) / size, "periods": chunk}
            )
    return candidates


def cheapest_window(periods: List[Dict[str, Any]], size: int) -> Optional[Dict[str, Any]]:
    candidates = candidate_windows(periods, size)
    return min(candidates, key=lambda item: item["value"]) if candidates else None


def priciest_window(periods: List[Dict[str, Any]], size: int) -> Optional[Dict[str, Any]]:
    candidates = candidate_windows(periods, size)
    return max(candidates, key=lambda item: item["value"]) if candidates else None


def _greedy_plan(
    periods: List[Dict[str, Any]],
    total_periods: int,
    window_count: int,
    min_window_periods: int,
) -> Optional[List[Dict[str, Any]]]:
    remaining = list(periods)
    selected: List[Dict[str, Any]] = []
    remaining_periods = total_periods
    remaining_windows = window_count

    while remaining_periods > 0 and remaining and remaining_windows > 0:
        size = math.ceil(remaining_periods / remaining_windows)
        size = min(max(size, min_window_periods), remaining_periods)

        best = cheapest_window(remaining, size)
        while best is None and size > 1:
            size -= 1
            best = cheapest_window(remaining, size)
        if best is None:
            return None

        selected.append(best)
        used = {(p["start"], p["end"]) for p in best["periods"]}
        remaining = [p for p in remaining if (p["start"], p["end"]) not in used]
        remaining_periods -= len(best["periods"])
        remaining_windows -= 1

    return selected if remaining_periods <= 0 else None


def _to_windows(plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    windows = [
        {
            "start": block["periods"][0]["start"],
            "stop": block["periods"][-1]["end"],
            "avg": block["value"],
            "period_count": len(block["periods"]),
        }
        for block in plan
    ]
    windows.sort(key=lambda item: item["start"])

    # Greedy selection can pick neighbouring blocks; present them as one session.
    merged: List[Dict[str, Any]] = []
    for window in windows:
        if merged and merged[-1]["stop"] == window["start"]:
            previous = merged[-1]
            total = previous["period_count"] + window["period_count"]
            previous["avg"] = (
                previous["avg"] * previous["period_count"]
                + window["avg"] * window["period_count"]
            ) / total
            previous["period_count"] = total
            previous["stop"] = window["stop"]
        else:
            merged.append(dict(window))
    return merged


def plan_windows(
    periods: List[Dict[str, Any]],
    total_periods: int,
    max_windows: int = 1,
    min_window_periods: int = 1,
    min_saving_ratio: float = 0.0,
) -> Dict[str, Any]:
    """Cheapest plan, only splitting into more sessions when the saving is worth it."""
    empty = {"windows": [], "cost": None, "baseline_cost": None, "saving": None}
    if total_periods <= 0 or not periods:
        return empty

    max_windows = max(1, int(max_windows))
    min_window_periods = max(1, int(min_window_periods))

    baseline_cost: Optional[float] = None
    best_plan: Optional[List[Dict[str, Any]]] = None
    best_cost: Optional[float] = None

    for window_count in range(1, max_windows + 1):
        if window_count > 1 and window_count * min_window_periods > total_periods:
            break

        plan = _greedy_plan(periods, total_periods, window_count, min_window_periods)
        if not plan:
            continue

        cost = sum(block["value"] * len(block["periods"]) for block in plan)
        if baseline_cost is None:
            baseline_cost = best_cost = cost
            best_plan = plan
            continue

        if cost < best_cost and cost <= baseline_cost * (1 - min_saving_ratio):
            best_cost = cost
            best_plan = plan

    if not best_plan:
        return empty

    windows = _to_windows(best_plan)
    return {
        "windows": windows,
        "cost": round(best_cost, 4),
        "baseline_cost": round(baseline_cost, 4),
        "saving": round((baseline_cost - best_cost) / baseline_cost, 4) if baseline_cost else 0.0,
    }


def weighted_average(windows: List[Dict[str, Any]]) -> Optional[float]:
    total = sum(window["period_count"] for window in windows)
    if total <= 0:
        return None
    return sum(window["avg"] * window["period_count"] for window in windows) / total

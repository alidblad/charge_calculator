# Charge calculator

This component finds the cheapest charging windows in the available Nordpool price horizon and starts **and stops** charging for you.

It recalculates on startup, on a timer, and whenever the Nordpool entity changes (which is how it picks up tomorrow's prices as soon as they are published around 13:00).

```yaml
charge_calculator:
	interval_minutes: 5
	nordpol_entity: sensor.nordpool_kwh_se3_sek_3_10_025

	house_battery:
		sensor_id: sensor.house_battery_soc
		size: 10
		min_charge_time: 1
		max_sessions: 2
		min_session_minutes: 60
		min_saving_ratio: 0.05
		break_even: true
		round_trip_efficiency: 0.88
		cycle_cost: 0.05

	car_battery:
		sensor_id: sensor.car_battery_soc
		size: 77
		min_charge_time: 1
		max_sessions: 2
		min_session_minutes: 60

	house_charge_action:
		service: huawei_solar.forcible_charge_soc
		data:
			device_id: 7c409407cc0b750d5734573138a8770f
			target_soc: "{stop_pct}"
			power: "{charge_power_w}"

	house_charge_stop_action:
		service: huawei_solar.stop_forcible_charge
		data:
			device_id: 7c409407cc0b750d5734573138a8770f
```

## Battery options

| Option | Default | Meaning |
| --- | --- | --- |
| `sensor_id` | – | State-of-charge sensor, in percent |
| `size` | – | Usable battery capacity in kWh |
| `min_charge_time` | `0` | Minimum total charge time in hours |
| `max_sessions` | `1` | Upper limit on charging windows per plan |
| `min_session_minutes` | `60` | Shortest allowed single window |
| `min_saving_ratio` | `0.03` | Extra windows are only used if they cut the total cost by at least this fraction |
| `break_even` | `false` | Skip charging when it cannot pay for itself |
| `round_trip_efficiency` | `0.9` | Charge/discharge efficiency, used by `break_even` |
| `cycle_cost` | `0.0` | Wear cost per kWh, used by `break_even` |

## How the windows are chosen

Charge need is converted to whole price periods (15 min when Nordpool reports quarters, otherwise hours), so no time is bought unnecessarily. The planner then compares a single continuous window against splitting into up to `max_sessions` windows, and only splits when the saving exceeds `min_saving_ratio`. On a flat price day you get one window; when there is a night dip and a midday dip you get two. Windows are always continuous in time.

With `break_even: true` the plan is dropped entirely if `charge_price / round_trip_efficiency + cycle_cost` is not below the average of the most expensive periods in the horizon — useful for the house battery, where a cheap-but-not-cheap-enough cycle loses money.

## Start and stop

`{label}_charge_action` is called when a window becomes active and `{label}_charge_stop_action` when it ends. The running session is persisted, so it survives a Home Assistant restart and is not re-triggered, and it is pinned while it runs so a price update cannot move the window out from under an active charge.

Available placeholders in both `data` blocks:

- `{label}`
- `{session_index}`
- `{start}` / `{stop}` (local ISO time)
- `{start_ts}` / `{stop_ts}`
- `{charge_hours}`
- `{stop_pct}`
- `{current_pct}`
- `{charge_power_kw}` / `{charge_power_w}`
- `{avg_price}`

The component still publishes these helper states for the active or next session:

- `charge_calculator.house_start_time`
- `charge_calculator.house_stop_time`
- `charge_calculator.car_start_time`
- `charge_calculator.car_stop_time`

## Entities

The integration creates real entities, so the plan is visible in the app and recorded in history.

| Entity | State | Useful attributes |
| --- | --- | --- |
| `sensor.charge_calculator_car_plan` / `..._house_battery_plan` | `charging`, `scheduled`, `idle`, `no_window`, `not_profitable`, `unavailable` | `reason`, `sessions`, `session_count`, `hours_needed`, `plan_cost`, `baseline_cost`, `saving_vs_single_window`, `effective_charge_price`, `expected_discharge_price` |
| `sensor.charge_calculator_car_next_charge_start` / `..._stop` | Timestamp | – |
| `sensor.charge_calculator_car_plan_price` | Average price of the planned windows | `plan_cost`, `baseline_cost`, `saving_vs_single_window` |
| `sensor.charge_calculator_current_price` | Current spot price | `prices` (whole horizon), `windows` (planned sessions), `price_min`, `price_max`, `price_avg` |
| `binary_sensor.charge_calculator_car_charging` | `on` while a session is running | `start`, `stop`, `minutes_remaining`, `target_pct`, `charge_power_kw` |

`sensor.charge_calculator_current_price` is meant for charting — its `prices` and `windows` attributes can be fed straight into an ApexCharts card to draw the price curve with the planned windows shaded.

The `plan` sensor always carries a `reason`, so when nothing is scheduled you can see why (target already reached, no window fits, or not profitable).

## Logging

Every run logs a one-line price summary, the charge need per battery, each chosen session in local time, and the outcome of the break-even check. Start and stop service calls are logged with the rendered data.

```yaml
logger:
	logs:
		custom_components.charge_calculator: info
```

Typical output:

```
--- Charge calculator run (execute_actions=True) ---
Prices: 36 periods of 60 min until Thu 02:00 (tomorrow_valid=True) | min 0.0900 @ Wed 05:00, max 1.2000 @ Tue 14:00, avg 1.0208
car: soc=55.0% target=80% -> 2.92 h (3 x 60 min periods) at 6.6 kW, max 2 session(s)
car: session 1/1 Wed 04:00 -> Wed 07:00 (3.00 h, avg 0.1000)
house: profitable, effective charge 0.1580 vs expected discharge 1.2000
ACTION START house: called huawei_solar.forcible_charge_soc (window Wed 05:00 -> Wed 07:00, 4.00 kW, target 90%, avg price 0.1) data={'target_soc': 90, 'power': 4000}
house session finished (Wed 05:00 -> Wed 07:00), stopping charge
ACTION STOP house: called huawei_solar.stop_forcible_charge ...
```

Set the level to `debug` to also get the full price table, every candidate window and its average price.

## Manual call

The service returns the full plan, so you can inspect it without acting on it:

```yaml
action: charge_calculator.calculate_charge
data:
	execute_actions: false
	house_charge_stop: 95
	house_charge_effect: 4
	house_max_sessions: 2
response_variable: plan
```

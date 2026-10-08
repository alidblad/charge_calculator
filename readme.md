# Charge calculator

Plans when to charge the car and the house battery from Nordpool spot prices, starts and stops
charging itself, and schedules house-battery discharge into the expensive hours.

It learns how fast the house battery drains and uses an SMHI weather forecast to avoid buying
energy that the sun is about to provide for free.

## Layout

| Module | Responsibility |
| --- | --- |
| `prices.py` | Normalised Nordpool horizon, period length, price statistics |
| `scheduler.py` | Cheap/expensive window selection, splitting, contiguity |
| `drain.py` | Learns the house consumption profile per hour and day type |
| `solar.py` | PV estimate from solar elevation and forecast cloud coverage |
| `strategies.py` | Car, house charge and house discharge planning |
| `coordinator.py` | Runs the strategies, fires actions, persists sessions, publishes plans |

Car and house share the price model but optimise for different things: the car is a
deadline-constrained cost minimisation, the house battery is arbitrage.

## Configuration

Create an `input_text` helper named **Car charge ready by** in Settings > Devices & services >
Helpers (entity ID `input_text.car_charge_ready_by`). Enter a local time such as `07:00` to
set a deadline, or leave it empty to charge in the cheapest available window. The helper can
also be defined in `configuration.yaml`:

```yaml
input_text:
	car_charge_ready_by:
		name: Car charge ready by
		max: 5
```

```yaml
charge_calculator:
	interval_minutes: 5
	nordpol_entity: sensor.nordpool_kwh_se3_sek_3_10_025

	weather_entity: weather.smhi_home
	solar_peak_kw: 8.0

	house_battery:
		sensor_id: sensor.house_battery_soc
		size: 10
		reserve_pct: 10
		max_sessions: 2
		min_session_minutes: 60
		min_saving_ratio: 0.05
		max_drain_kw: 50.0
		break_even: true
		round_trip_efficiency: 0.88
		cycle_cost: 0.05
		load_entity: sensor.house_power          # optional, better than learning from SoC
		discharge: true
		discharge_effect: 4.0
		min_discharge_spread: 0.20

	car_battery:
		sensor_id: sensor.car_battery_soc
		size: 77
		max_sessions: 2
		min_session_minutes: 60
		ready_by_entity: input_text.car_charge_ready_by
		plugged_in_entity: binary_sensor.car_cable_connected

	house_charge_action:
		service: huawei_solar.forcible_charge_soc
		data:
			device_id: 7c409407cc0b750d5734573138a8770f
			target_soc: "{target_pct}"
			power: "{charge_power_w}"
	house_charge_stop_action:
		service: huawei_solar.stop_forcible_charge
		data:
			device_id: 7c409407cc0b750d5734573138a8770f

	house_discharge_action:
		service: huawei_solar.forcible_discharge
		data:
			device_id: 7c409407cc0b750d5734573138a8770f
			power: "{charge_power_w}"
	house_discharge_stop_action:
		service: huawei_solar.stop_forcible_discharge
		data:
			device_id: 7c409407cc0b750d5734573138a8770f

	car_charge_action:
		service: switch.turn_on
		data:
			entity_id: switch.car_charger
	car_charge_stop_action:
		service: switch.turn_off
		data:
			entity_id: switch.car_charger
```

### Battery options

| Option | Default | Applies to | Meaning |
| --- | --- | --- | --- |
| `sensor_id` | – | both | State-of-charge sensor in percent |
| `size` | – | both | Usable capacity in kWh |
| `min_charge_time` | `0` | both | Minimum total charge time in hours |
| `max_sessions` | `1` | both | Upper limit on windows per plan |
| `min_session_minutes` | `60` | both | Shortest allowed single window |
| `min_saving_ratio` | `0.03` | both | Extra windows only used if they cut cost by this fraction |
| `enable_entity` | – | both | Optional external toggle; scheduling is off while it is off |
| `ready_by` | – | car | Optional fixed local deadline, e.g. `"07:00"` |
| `ready_by_entity` | – | car | Optional `input_text` helper for a GUI-editable deadline; overrides `ready_by` |
| `plugged_in_entity` | – | car | Charging is blocked unless this is on |
| `reserve_pct` | `10` | house | Floor the battery is never planned below || `break_even` | `false` | house | Skip charging that cannot pay for itself |
| `max_drain_kw` | `50` | house | Ignore drain samples above this limit and clear invalid stored samples |
| `round_trip_efficiency` | `0.9` | house | Charge/discharge efficiency |
| `cycle_cost` | `0.0` | house | Wear cost per kWh |
| `load_entity` | – | house | Instantaneous house-load power sensor in `W` or `kW`; more accurate than learning from SoC |
| `discharge` | `false` | house | Enable discharge scheduling |
| `discharge_enable_entity` | – | house | Optional external toggle for discharge |
| `discharge_effect` | `4.0` | house | Discharge power in kW |
| `min_discharge_spread` | `0.20` | house | Required margin over the charge price |

## How it decides

**Enabling and disabling.** Each battery has a `switch.charge_calculator_..._scheduling`
entity, on by default and remembered across restarts. Turning one off stops any session that
is currently running (the configured stop action is sent immediately) and skips planning until
it is turned back on. The plan status becomes `disabled`. If you would rather drive this from
an existing `input_boolean`, point `enable_entity` at it; both must be on for scheduling to
run.

**Windows.** Charge need is converted to whole price periods (15 min when Nordpool reports
quarters), so no time is bought unnecessarily. The planner compares one continuous window
against splitting into up to `max_sessions`, and only splits when the saving exceeds
`min_saving_ratio`. A flat day gives one window; a night dip plus a midday dip gives two.
Windows are always contiguous in time.

**Car.** Charging is blocked unless `plugged_in_entity` is on. Windows are restricted to
periods that finish before `ready_by`; if the car cannot reach its target in time the deadline
is relaxed and logged rather than silently under-charging. With `ready_by_entity`, enter a
local time (`HH:MM`) in the helper to set the deadline; clearing it removes the deadline and
replans immediately. With neither deadline option set, the cheapest window in the available
price horizon is chosen. An active charging session remains pinned until its planned stop.

**House.** The drain profile gives an estimated time until the battery hits `reserve_pct`, and
that becomes the deadline. Expected PV surplus (production minus predicted house load) is
subtracted from the energy to buy, so a sunny tomorrow means charging less — or nothing. With
`break_even: true` the plan is dropped if `charge_price / round_trip_efficiency + cycle_cost`
is not below the average of the most expensive periods in the horizon. If the reserve deadline
cannot fit a full charge session, the planner prefers the earliest contiguous full session in the
horizon and marks `deadline_relaxed`. If no such session exists, it falls back to the cheapest
available window.

**Discharge.** With `discharge: true` the priciest contiguous block that does not overlap a
planned charge window is selected, and used only if `discharge_price - (charge_price /
efficiency + cycle_cost)` clears `min_discharge_spread`.

**Drain learning.** Consumption is bucketed by hour of day and weekday/weekend, updated with an
exponential moving average and persisted across restarts. A flat average would mis-predict
badly, since evening load is far above night load. If `load_entity` is set it is used directly;
it must report instantaneous power in `W` or `kW`, not energy in `Wh` or `kWh`. Otherwise samples
are taken from falling state of charge.

**Solar.** SMHI publishes no irradiance, so production is approximated from solar elevation
(via HA's astral location) scaled by forecast `cloud_coverage`. It is only accurate enough to
answer "will solar refill the battery tomorrow?", which is all the planner asks of it. Set
`solar_peak_kw` to the realistic peak output of your array; leave it out to disable.

## Start and stop

`*_charge_action` and `*_discharge_action` fire when a window becomes active, the matching
`*_stop_action` when it ends. The running session is persisted, so it survives a restart
without being re-triggered, and is pinned while it runs so a price update cannot move the
window out from under an active session.

Placeholders available in every `data` block:

`{task}`, `{session_index}`, `{start}`, `{stop}`, `{start_ts}`, `{stop_ts}`, `{charge_hours}`,
`{target_pct}` (also `{stop_pct}`), `{reserve_pct}`, `{current_pct}`, `{charge_power_kw}`,
`{charge_power_w}`, `{avg_price}`.

## Entities

| Entity | State | Useful attributes |
| --- | --- | --- |
| `sensor.charge_calculator_{car,house_battery,house_battery_discharge}_plan` | `charging`, `discharging`, `scheduled`, `idle`, `blocked`, `no_window`, `not_profitable`, `unavailable` | `reason`, `sessions`, `energy_needed_kwh`, `solar_offset_kwh`, `hours_until_reserve`, `plan_cost`, `baseline_cost`, `spread` |
| `sensor.charge_calculator_..._next_start` / `_next_stop` | Timestamp | – |
| `sensor.charge_calculator_..._plan_price` | Average price of the planned windows | `plan_cost`, `baseline_cost`, `saving_vs_single_window` |
| `sensor.charge_calculator_current_price` | Current spot price | `prices` (whole horizon), `windows` (all planned sessions) |
| `sensor.charge_calculator_house_drain_rate` | Learned kW for this hour | `profile_weekday`, `profile_weekend`, `learned_hours`, `source` |
| `sensor.charge_calculator_house_battery_runway` | Hours until the reserve | `empty_at`, `reserve_pct` |
| `sensor.charge_calculator_solar_forecast_today` | Expected kWh left today | `tomorrow_kwh`, `cloud_now` |
| `binary_sensor.charge_calculator_..._active` | `on` while a session runs | `start`, `stop`, `minutes_remaining` |
| `switch.charge_calculator_{car,house_battery,house_battery_discharge}_scheduling` | `on` when scheduling is allowed | `external_enable_entity`, `effectively_enabled` |

The `plan` sensors always carry a `reason`, so when nothing is scheduled you can see why.
`sensor.charge_calculator_current_price` is meant for charting: its `prices` and `windows`
attributes feed straight into an ApexCharts card.

The drain sensor starts out empty and becomes useful after a day or two of learning. Until it
has data the house battery simply plans without a deadline.

Use `sensor.charge_calculator_car_next_stop` in automations; it is a proper timestamp entity.
If you configure `car_charge_action` / `car_charge_stop_action`, the integration starts
and stops charging itself and any automation doing that becomes redundant.

## Logging

```yaml
logger:
	logs:
		custom_components.charge_calculator: info
```

```
--- Charge calculator run (execute=True) ---
Prices: 36 periods of 60 min until Thu 02:00 (tomorrow_valid=True) | min 0.0900 @ Wed 05:00, max 2.8000 @ Tue 20:00, avg 1.2208
House drain: 1.000 kW now, 24 of 48 hour buckets learned (from state of charge)
Solar forecast: 0.06 kWh left today, 24.20 kWh tomorrow (cloud 80%)
house: soc=60.0% target=90% -> 0.74 h (1 periods) at 4.0 kW
house: solar is expected to contribute 0.06 kWh, buying that much less
house: battery reaches its reserve in 5.0 h (Tue 19:00)
house: session 1/1 Tue 14:00 -> Tue 15:00 (1.00 h, avg 1.2000)
house_discharge: session 1/1 Tue 20:00 -> Tue 22:00 (2.00 h, avg 2.6500)
ACTION START car: called switch.turn_on (window Wed 04:00 -> Wed 07:00, 6.60 kW, avg price 0.1) data={...}
car session finished (Wed 04:00 -> Wed 07:00), sending stop
```

Set the level to `debug` for the full price table and every candidate window.

## Manual call

```yaml
action: charge_calculator.calculate_charge
data:
	execute_actions: false
	house_charge_stop: 95
	house_max_sessions: 2
response_variable: plan
```

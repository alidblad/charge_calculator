# Charge calcuator

This component calculates the cheapest upcoming charge window and can now also start charging automatically.

Add the integration configuration in your Home Assistant configuration and define the charger service you want it to call for each battery. The integration recalculates immediately on startup and then every 5 minutes by default.

```yaml
charge_calculator:
	interval_minutes: 5
	nordpol_entity: sensor.nordpool_kwh_se3_sek_3_10_025

	house_battery:
		sensor_id: sensor.house_battery_soc
		size: 10
		min_charge_time: 1
		max_sessions: 2

	car_battery:
		sensor_id: sensor.car_battery_soc
		size: 77
		min_charge_time: 1
		max_sessions: 2

	house_charge_action:
		service: huawei_solar.forcible_charge_soc
		data:
			device_id: 7c409407cc0b750d5734573138a8770f
			target_soc: "{stop_pct}"
			power: "{charge_power_w}"
```

Available placeholders in `house_charge_action.data` and `car_charge_action.data`:

- `{label}`
- `{session_index}`
- `{start}`
- `{stop}`
- `{start_ts}`
- `{stop_ts}`
- `{charge_hours}`
- `{stop_pct}`
- `{current_pct}`
- `{charge_power_kw}`
- `{charge_power_w}`

If the current time falls inside the calculated start/stop window, the configured service is called once for that schedule. The component still publishes these helper states:

- `charge_calculator.house_start_time`
- `charge_calculator.house_stop_time`
- `charge_calculator.car_start_time`
- `charge_calculator.car_stop_time`

When `max_sessions` is set to `2` or `3`, the component can split one day into multiple cheap charging windows instead of forcing one continuous block.

You can still call the service manually:

```yaml
service: charge_calculator.calculate_charge
data:
	execute_actions: true
	house_charge_stop: 95
	house_charge_effect: 4
	house_max_sessions: 2
```

# Better Climate

Better Climate is a Home Assistant custom integration that controls an air
conditioner and an optional boiler from a separate room temperature sensor.
It exposes one climate entity with shared temperature controls and the modes
`off`, `cool`, and, when a boiler is configured, `heat` and `heat_cool`.

## Why

The temperature sensor built into an air conditioner or boiler often measures
the temperature near the appliance rather than the temperature where people
actually are. Better Climate keeps the user-selected room sensor as the source
of truth while using each appliance's native thermostat to request or stop
conditioning.

## Features

- External room temperature sensor for both cooling and heating
- One target for cooling or heating, plus a target range for `heat_cool`
- Optional boiler support
- Automatic heat/cool range control that retains the selected mode in idle
- Optional fan coordination, including directional and variable-speed fans
- Cooling and heating interlock
- Configurable hysteresis and source offset
- Minimum command interval to reduce repeated device commands
- Synchronization with mode, power, and target changes made on the original
  climate entities
- Safe fallback to the source thermostat when a required temperature reading is
  unavailable
- UI-based setup and reconfiguration

Swing, presets, humidity, and outdoor-temperature controls remain on the
original entities.

## How It Works

Better Climate compares the external room temperature with the virtual target.
It then shifts the active source's target around that source's internal
temperature:

| State | Source target |
| --- | --- |
| Cooling required | Internal temperature minus the source offset |
| Cooling satisfied | Internal temperature plus the source offset |
| Heating required | Internal temperature plus the source offset |
| Heating satisfied | Internal temperature minus the source offset |

Hysteresis prevents rapid switching around the target. Changing from cooling to
heating, or from heating to cooling, turns off and confirms the opposite source
before enabling the selected source.

When demand is satisfied, Better Climate leaves the selected source on and
shifts its target so the source's native thermostat can idle without repeated
power cycles. In `heat_cool`, the selected cooling or heating mode is retained
while idle and changes only after the room crosses the opposite boundary plus
hysteresis. The initial range is `22 °C` to `25 °C`; user changes are restored
after restart.

## Requirements

- Home Assistant 2026.7 or newer
- A `climate` entity that supports `cool`
- A temperature `sensor` entity
- Optionally, a different `climate` entity that supports `heat`
- Overlapping target-temperature ranges when both sources are configured

## Installation

### HACS

1. Open HACS.
2. Add `https://github.com/yoonpooh/better-climate` as a custom integration
   repository.
3. Install **Better Climate**.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/better_climate` into the
   `/config/custom_components` directory.
2. Restart Home Assistant.

## Configuration

Go to **Settings > Devices & services > Add integration**, search for
**Better Climate**, and configure:

| Option | Description | Default |
| --- | --- | --- |
| Name | Name of the virtual climate entity | `Better Climate` |
| Air conditioner | Climate entity used for cooling | Required |
| Boiler | Climate entity used for heating | Optional |
| Fan | Kept on with HVAC; directional fans follow cooling/heating and variable-speed fans increase one step per 0.5 °C of demand | Optional |
| Room temperature sensor | External room temperature | Required |
| Room temperature hysteresis | Difference required before changing demand | `0.5 °C` |
| Source force offset | Adjustment applied around the source temperature | `0.5 °C` |
| Minimum command interval | Minimum time between source target commands | `30 s` |

The resulting entity is cooling-only when no boiler is selected.

To edit an existing configuration, open **Settings > Devices & services >
Better Climate**, open the top-right menu for the configuration entry, and
select **Reconfigure**. Existing values are prefilled and the entry reloads
after saving.

## Safety Behavior

- Cooling and heating are mutually exclusive.
- A configured fan stays on while the active HVAC mode is idle and turns off only
  when the HVAC mode turns off. Directional ceiling fans run forward for cooling
  and reverse for heating. Variable-speed fans use their lowest step within
  `0.5 °C` of the target and increase one step for each additional `0.5 °C`.
  A `0.1 °C` speed hysteresis prevents sensor jitter from repeatedly changing
  adjacent fan levels.
- Fans without speed control continue to use direction and power control only.
- A fan that is already running when a new Better Climate session starts is
  adopted by that session and turns off with HVAC. Startup restoration alone
  does not adopt a manually running fan.
- Manually turning off a fan keeps it off until the current HVAC session ends.
- Turning off Better Climate attempts to turn off every configured source.
- Sources with native power controls are powered on before their HVAC mode is
  selected and use their native power-off service when stopped.
- A failed explicit power-off is retried every 30 seconds until it succeeds or
  the climate source is turned on again.
- Idle keeps the selected cooling or heating source powered and uses its native
  thermostat to avoid short power cycles.
- Invalid or unavailable sensor readings stop external correction and restore
  the virtual target to the active source.
- A restored entity keeps its last target, active mode, and fan ownership.

## Development

Run the tests against the supported Home Assistant version:

```bash
uv run --python 3.14 --with homeassistant==2026.7.4 \
  python -m unittest discover -s tests -v
```

Run static checks:

```bash
uvx ruff check .
uvx ruff format --check .
uv run --python 3.14 --with homeassistant==2026.7.4 --with mypy \
  mypy custom_components/better_climate --ignore-missing-imports
```

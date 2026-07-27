"""Focused tests for Better Climate configuration validation."""

import unittest
from types import SimpleNamespace

try:
    from homeassistant.components.climate.const import HVACMode
    from homeassistant.core import State
except ModuleNotFoundError as err:
    raise unittest.SkipTest("Home Assistant is not installed") from err

from custom_components.better_climate.config_flow import BetterClimateConfigFlow
from custom_components.better_climate.const import (
    CONF_COOLING_ENTITY,
    CONF_FAN,
    CONF_TEMPERATURE_SENSOR,
)

COOLING = "climate.cooling"
SENSOR = "sensor.room_temperature"
FAN = "fan.ceiling"


class ConfigFlowValidationTest(unittest.TestCase):
    """Verify the optional fan capability boundary."""

    def test_generic_fan_does_not_require_direction_control(self) -> None:
        states = {
            COOLING: State(
                COOLING,
                HVACMode.OFF,
                {
                    "hvac_modes": [HVACMode.OFF, HVACMode.COOL],
                    "current_temperature": 24,
                },
            ),
            SENSOR: State(SENSOR, "24"),
            FAN: State(FAN, "off"),
        }
        flow = SimpleNamespace(
            hass=SimpleNamespace(states=SimpleNamespace(get=states.get))
        )
        data = {
            CONF_COOLING_ENTITY: COOLING,
            CONF_TEMPERATURE_SENSOR: SENSOR,
            CONF_FAN: FAN,
        }

        self.assertEqual(BetterClimateConfigFlow._validate(flow, data), {})

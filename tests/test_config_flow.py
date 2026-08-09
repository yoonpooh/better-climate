"""Focused tests for Better Climate configuration validation."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

try:
    from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
    from homeassistant.const import ATTR_SUPPORTED_FEATURES
    from homeassistant.core import State
except ModuleNotFoundError as err:
    raise unittest.SkipTest("Home Assistant is not installed") from err

from custom_components.better_climate.config_flow import BetterClimateConfigFlow
from custom_components.better_climate.const import (
    CONF_COOLING_ENTITY,
    CONF_FAN,
    CONF_FORCE_OFFSET,
    CONF_HYSTERESIS,
    CONF_MIN_COMMAND_INTERVAL,
    CONF_POWER_OFF_WHEN_COOLING_IDLE,
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
                    ATTR_SUPPORTED_FEATURES: int(
                        ClimateEntityFeature.TARGET_TEMPERATURE
                    ),
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

    def test_climate_source_requires_off_and_target_temperature(self) -> None:
        states = {
            COOLING: State(
                COOLING,
                HVACMode.COOL,
                {
                    "hvac_modes": [HVACMode.COOL],
                    "current_temperature": 24,
                    ATTR_SUPPORTED_FEATURES: int(
                        ClimateEntityFeature.TARGET_TEMPERATURE
                    ),
                },
            ),
            SENSOR: State(SENSOR, "24"),
        }
        flow = SimpleNamespace(
            hass=SimpleNamespace(states=SimpleNamespace(get=states.get))
        )
        data = {
            CONF_COOLING_ENTITY: COOLING,
            CONF_TEMPERATURE_SENSOR: SENSOR,
        }

        self.assertEqual(
            BetterClimateConfigFlow._validate(flow, data)[CONF_COOLING_ENTITY],
            "off_not_supported",
        )

        states[COOLING] = State(
            COOLING,
            HVACMode.OFF,
            {
                "hvac_modes": [HVACMode.OFF, HVACMode.COOL],
                "current_temperature": 24,
                ATTR_SUPPORTED_FEATURES: 0,
            },
        )
        self.assertEqual(
            BetterClimateConfigFlow._validate(flow, data)[CONF_COOLING_ENTITY],
            "target_temperature_not_supported",
        )


class ConfigFlowReconfigureTest(unittest.IsolatedAsyncioTestCase):
    """Verify an existing configuration can be replaced and reloaded."""

    async def test_reconfigure_updates_entry_data_and_reloads(self) -> None:
        entry = SimpleNamespace(data={"name": "Old"})
        updated = {
            "name": "Living room",
            CONF_COOLING_ENTITY: COOLING,
            CONF_TEMPERATURE_SENSOR: SENSOR,
            CONF_FAN: FAN,
            CONF_HYSTERESIS: 0.3,
            CONF_FORCE_OFFSET: 0.5,
            CONF_MIN_COMMAND_INTERVAL: 10,
            CONF_POWER_OFF_WHEN_COOLING_IDLE: True,
        }
        result = {"type": "abort", "reason": "reconfigure_successful"}
        flow = SimpleNamespace(
            _get_reconfigure_entry=Mock(return_value=entry),
            _validate=Mock(return_value={}),
            async_set_unique_id=AsyncMock(),
            _abort_if_unique_id_mismatch=Mock(),
            async_update_reload_and_abort=Mock(return_value=result),
        )

        self.assertEqual(
            await BetterClimateConfigFlow.async_step_reconfigure(flow, updated),
            result,
        )
        flow.async_set_unique_id.assert_awaited_once_with(COOLING)
        flow._abort_if_unique_id_mismatch.assert_called_once_with()
        flow.async_update_reload_and_abort.assert_called_once_with(
            entry,
            title="Living room",
            data=updated,
        )

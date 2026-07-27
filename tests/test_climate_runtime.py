"""Focused entity-runtime tests for Better Climate."""

import asyncio
import unittest
from collections import deque
from time import monotonic
from types import SimpleNamespace

try:
    from homeassistant.components.climate.const import HVACMode
    from homeassistant.core import Event, State
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError as err:
    raise unittest.SkipTest("Home Assistant is not installed") from err

from custom_components.better_climate.climate import BetterClimate
from custom_components.better_climate.const import (
    CONF_COOLING_ENTITY,
    CONF_FORCE_OFFSET,
    CONF_HEATING_ENTITY,
    CONF_HYSTERESIS,
    CONF_MIN_COMMAND_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
)

COOLING = "climate.cooling"
HEATING = "climate.heating"
SENSOR = "sensor.room_temperature"


class FakeStates:
    """Minimal Home Assistant state store."""

    def __init__(self, states: dict[str, State]) -> None:
        self._states = states

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class FakeServices:
    """Record service calls without touching devices."""

    def __init__(self) -> None:
        self.calls = []

    async def async_call(
        self, domain: str, service: str, data: dict, *, blocking: bool
    ) -> None:
        self.calls.append((domain, service, data, blocking))


def climate_state(entity_id: str, state: str, **attributes) -> State:
    """Build a source climate state."""
    defaults = {
        "current_temperature": 25,
        "temperature": 24,
        "min_temp": 18 if entity_id == COOLING else 5,
        "max_temp": 30 if entity_id == COOLING else 40,
        "target_temp_step": 0.5,
    }
    return State(entity_id, state, {**defaults, **attributes})


def make_entity(
    *,
    cooling_state: str = HVACMode.OFF,
    heating_state: str = HVACMode.OFF,
    sensor_state: str = "24",
) -> tuple[BetterClimate, FakeServices]:
    """Create a Better Climate entity with in-memory sources."""
    services = FakeServices()
    hass = SimpleNamespace(
        states=FakeStates(
            {
                COOLING: climate_state(COOLING, cooling_state),
                HEATING: climate_state(HEATING, heating_state),
                SENSOR: State(SENSOR, sensor_state),
            }
        ),
        services=services,
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
    )
    entry = SimpleNamespace(
        data={
            "name": "Room",
            CONF_COOLING_ENTITY: COOLING,
            CONF_HEATING_ENTITY: HEATING,
            CONF_TEMPERATURE_SENSOR: SENSOR,
            CONF_HYSTERESIS: 0.3,
            CONF_FORCE_OFFSET: 0.5,
            CONF_MIN_COMMAND_INTERVAL: 30,
        },
        entry_id="test",
    )
    entity = BetterClimate(hass, entry)
    entity.async_write_ha_state = lambda: None
    entity._target_temperature = 24
    return entity, services


class BetterClimateRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Verify safety behavior around the pure controller."""

    async def test_sensor_failure_restores_virtual_target(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="unavailable",
        )
        entity._cooling_required = True
        entity.hass.states._states[COOLING] = climate_state(
            COOLING, HVACMode.COOL, temperature=18
        )

        await entity._async_reconcile()

        self.assertFalse(entity._cooling_required)
        self.assertEqual(
            services.calls,
            [
                (
                    "climate",
                    "set_temperature",
                    {"entity_id": COOLING, "temperature": 24.0},
                    True,
                )
            ],
        )

    async def test_turn_off_attempts_both_sources_after_failure(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.COOL,
            heating_state=HVACMode.HEAT,
        )
        calls = []

        async def turn_source_off(entity_id: str) -> None:
            calls.append(entity_id)
            if entity_id == COOLING:
                raise HomeAssistantError("offline")

        entity._async_turn_source_off = turn_source_off

        with self.assertRaises(HomeAssistantError):
            await entity._async_turn_off_locked()

        self.assertCountEqual(calls, [COOLING, HEATING])

    async def test_startup_resolves_dual_active_sources(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.COOL,
            heating_state=HVACMode.HEAT,
        )
        entity._last_active_mode = HVACMode.HEAT
        turned_off = []

        async def turn_source_off(entity_id: str) -> None:
            turned_off.append(entity_id)

        async def reconcile(*, force: bool = False) -> None:
            self.assertTrue(force)

        entity._async_turn_source_off = turn_source_off
        entity._async_reconcile = reconcile

        await entity._async_initialize_control()

        self.assertEqual(turned_off, [COOLING])

    async def test_startup_initializes_missing_target(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._target_temperature = None

        async def reconcile(*, force: bool = False) -> None:
            self.assertTrue(force)

        entity._async_reconcile = reconcile

        await entity._async_initialize_control()

        self.assertEqual(entity.target_temperature, 24)

    async def test_off_is_available_when_heating_is_available(self) -> None:
        entity, _services = make_entity()
        entity.hass.states._states[COOLING] = State(COOLING, "unavailable")

        self.assertTrue(entity.available)

    async def test_mode_transitions_are_serialized(self) -> None:
        entity, _services = make_entity()
        active = 0
        maximum_active = 0

        async def transition() -> None:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1

        entity._async_activate_cooling_locked = transition
        entity._async_activate_heating_locked = transition

        await asyncio.gather(
            entity._async_activate_cooling(),
            entity._async_activate_heating(),
        )

        self.assertEqual(maximum_active, 1)

    async def test_non_finite_sensor_uses_safe_fallback(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="nan",
        )
        entity.hass.states._states[COOLING] = climate_state(
            COOLING, HVACMode.COOL, temperature=18
        )

        await entity._async_reconcile()

        self.assertEqual(
            services.calls[0][2],
            {"entity_id": COOLING, "temperature": 24.0},
        )

    async def test_delayed_source_event_does_not_change_virtual_target(
        self,
    ) -> None:
        entity, _services = make_entity()
        entity._expected_source_temperatures[COOLING] = deque(
            [(22.5, monotonic())], maxlen=8
        )
        unchanged = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.COOL, temperature=24),
                "new_state": climate_state(COOLING, HVACMode.COOL, temperature=24),
            },
        )
        expected = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.COOL, temperature=24),
                "new_state": climate_state(COOLING, HVACMode.COOL, temperature=22.5),
            },
        )

        self.assertFalse(entity._sync_target_from_event(unchanged))
        self.assertEqual(len(entity._expected_source_temperatures[COOLING]), 1)
        self.assertFalse(entity._sync_target_from_event(expected))
        self.assertNotIn(COOLING, entity._expected_source_temperatures)
        self.assertEqual(entity.target_temperature, 24)

"""Focused entity-runtime tests for Better Climate."""

import asyncio
import unittest
from collections import deque
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from homeassistant.components.climate.const import (
        ClimateEntityFeature,
        HVACAction,
        HVACMode,
    )
    from homeassistant.components.fan import (
        ATTR_PERCENTAGE,
        ATTR_PERCENTAGE_STEP,
        FanEntityFeature,
    )
    from homeassistant.const import ATTR_SUPPORTED_FEATURES
    from homeassistant.core import Event, State
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError as err:
    raise unittest.SkipTest("Home Assistant is not installed") from err

from custom_components.better_climate.climate import (
    ATTR_FAN_MANUAL_OFF_MODE,
    ATTR_FAN_OWNED,
    ATTR_IDLE_SOURCE_MODE,
    SOURCE_OFF_RETRY_INTERVAL,
    BetterClimate,
)
from custom_components.better_climate.const import (
    CONF_COOLING_ENTITY,
    CONF_FAN,
    CONF_FORCE_OFFSET,
    CONF_HEATING_ENTITY,
    CONF_HYSTERESIS,
    CONF_MIN_COMMAND_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
)

COOLING = "climate.cooling"
HEATING = "climate.heating"
SENSOR = "sensor.room_temperature"
FAN = "fan.ceiling"


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
    fan_state: str | None = None,
    fan_direction: str = "forward",
    fan_supports_direction: bool = True,
    fan_percentage: int | None = None,
    fan_percentage_step: float = 25,
) -> tuple[BetterClimate, FakeServices]:
    """Create a Better Climate entity with in-memory sources."""
    services = FakeServices()
    states = {
        COOLING: climate_state(COOLING, cooling_state),
        HEATING: climate_state(HEATING, heating_state),
        SENSOR: State(SENSOR, sensor_state),
    }
    if fan_state is not None:
        features = (
            FanEntityFeature.DIRECTION
            if fan_supports_direction
            else FanEntityFeature(0)
        )
        fan_attributes = {"direction": fan_direction}
        if fan_percentage is not None:
            features |= FanEntityFeature.SET_SPEED
            fan_attributes.update(
                {
                    ATTR_PERCENTAGE: fan_percentage,
                    ATTR_PERCENTAGE_STEP: fan_percentage_step,
                }
            )
        states[FAN] = State(
            FAN,
            fan_state,
            {
                **fan_attributes,
                ATTR_SUPPORTED_FEATURES: int(features),
            },
        )
    data = {
        "name": "Room",
        CONF_COOLING_ENTITY: COOLING,
        CONF_HEATING_ENTITY: HEATING,
        CONF_TEMPERATURE_SENSOR: SENSOR,
        CONF_HYSTERESIS: 0.3,
        CONF_FORCE_OFFSET: 0.5,
        CONF_MIN_COMMAND_INTERVAL: 30,
    }
    if fan_state is not None:
        data[CONF_FAN] = FAN
    hass = SimpleNamespace(
        states=FakeStates(states),
        services=services,
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
        loop=asyncio.get_running_loop(),
    )
    entry = SimpleNamespace(
        data=data,
        entry_id="test",
    )
    entity = BetterClimate(hass, entry)
    entity.async_write_ha_state = lambda: None
    entity._target_temperature = 24
    return entity, services


class BetterClimateRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Verify safety behavior around the pure controller."""

    async def test_default_heat_cool_range_is_22_to_25(self) -> None:
        entity, _services = make_entity()

        entity._ensure_target_range()

        self.assertEqual(entity.target_temperature_low, 22)
        self.assertEqual(entity.target_temperature_high, 25)

    async def test_idle_source_mode_survives_entity_restoration(self) -> None:
        entity, _services = make_entity()
        entity.async_get_last_state = AsyncMock(
            return_value=State(
                "climate.room",
                HVACMode.COOL,
                {
                    ATTR_IDLE_SOURCE_MODE: HVACMode.COOL,
                    "temperature": 24,
                },
            )
        )
        entity.async_on_remove = lambda _remove: None
        entity.hass.async_create_task = lambda coro: coro.close()

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity.async_added_to_hass()

        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)

    async def test_active_restored_mode_migrates_to_mode_marker(self) -> None:
        entity, _services = make_entity()
        entity.async_get_last_state = AsyncMock(
            return_value=State(
                "climate.room",
                HVACMode.COOL,
                {"temperature": 24},
            )
        )
        entity.async_on_remove = lambda _remove: None
        entity.hass.async_create_task = lambda coro: coro.close()

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity.async_added_to_hass()

        self.assertEqual(entity._idle_source_mode, HVACMode.COOL)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)

    async def test_fan_ownership_survives_entity_restoration(self) -> None:
        entity, services = make_entity(fan_state="on")
        entity.async_get_last_state = AsyncMock(
            return_value=State(
                "climate.room",
                HVACMode.OFF,
                {
                    ATTR_FAN_OWNED: True,
                    "last_active_mode": HVACMode.COOL,
                    "temperature": 24,
                },
            )
        )
        entity.async_on_remove = lambda _remove: None
        entity.hass.async_create_task = lambda coro: coro.close()

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity.async_added_to_hass()

        self.assertTrue(entity._fan_owned)
        self.assertTrue(entity.extra_state_attributes[ATTR_FAN_OWNED])

        await entity._async_sync_ceiling_fan(HVACMode.OFF)

        self.assertEqual(
            services.calls,
            [("fan", "turn_off", {"entity_id": FAN}, True)],
        )

    async def test_manual_fan_off_survives_entity_restoration(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            fan_state="off",
        )
        entity.async_get_last_state = AsyncMock(
            return_value=State(
                "climate.room",
                HVACMode.COOL,
                {
                    ATTR_FAN_MANUAL_OFF_MODE: HVACMode.COOL,
                    "temperature": 24,
                },
            )
        )
        entity.async_on_remove = lambda _remove: None
        entity.hass.async_create_task = lambda coro: coro.close()

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity.async_added_to_hass()

        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(services.calls, [])
        self.assertEqual(entity._fan_manual_off_mode, HVACMode.COOL)

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

        async def turn_source_off(entity_id: str, *, force: bool = False) -> None:
            calls.append(entity_id)
            self.assertTrue(force)
            if entity_id == COOLING:
                raise HomeAssistantError("offline")

        entity._async_turn_source_off = turn_source_off

        with self.assertRaises(HomeAssistantError):
            await entity._async_turn_off_locked()

        self.assertCountEqual(calls, [COOLING, HEATING])

    async def test_turn_off_failure_retries_until_it_succeeds(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._async_turn_source_off = AsyncMock(
            side_effect=[HomeAssistantError("offline"), None, None, None]
        )
        tasks = []
        entity.hass.async_create_task = tasks.append

        with patch(
            "custom_components.better_climate.climate.async_call_later",
            return_value=lambda: None,
        ) as call_later:
            with self.assertRaises(HomeAssistantError):
                await entity.async_turn_off()

            self.assertEqual(call_later.call_args.args[1], SOURCE_OFF_RETRY_INTERVAL)
            call_later.call_args.args[2](None)
            await tasks.pop()

        self.assertEqual(entity._async_turn_source_off.await_count, 4)
        self.assertIsNone(entity._source_off_retry_timer)

    async def test_power_services_wrap_source_mode_changes(self) -> None:
        entity, services = make_entity(cooling_state=HVACMode.OFF)
        entity.hass.states._states[COOLING] = climate_state(
            COOLING,
            HVACMode.OFF,
            supported_features=int(
                ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
            ),
        )

        async def call_service(
            domain: str, service: str, data: dict, *, blocking: bool
        ) -> None:
            services.calls.append((domain, service, data, blocking))
            if service == "turn_on":
                entity.hass.states._states[COOLING] = climate_state(
                    COOLING,
                    "dry",
                    supported_features=int(
                        ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
                    ),
                )
            elif service == "turn_off":
                entity.hass.states._states[COOLING] = climate_state(
                    COOLING,
                    HVACMode.OFF,
                    supported_features=int(
                        ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
                    ),
                )

        services.async_call = call_service

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity._async_set_source_mode(COOLING, HVACMode.COOL)
            await entity._async_turn_source_off(COOLING, force=True)

        self.assertEqual(
            services.calls,
            [
                ("climate", "turn_on", {"entity_id": COOLING}, True),
                (
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": COOLING, "hvac_mode": HVACMode.COOL},
                    True,
                ),
                ("climate", "turn_off", {"entity_id": COOLING}, True),
            ],
        )

    async def test_cached_off_source_is_only_forced_off_explicitly(self) -> None:
        entity, services = make_entity(cooling_state=HVACMode.OFF)
        entity.hass.states._states[COOLING] = climate_state(
            COOLING,
            HVACMode.OFF,
            supported_features=int(ClimateEntityFeature.TURN_OFF),
        )

        with patch(
            "custom_components.better_climate.climate.async_track_state_change_event",
            return_value=lambda: None,
        ):
            await entity._async_turn_source_off(COOLING)
            await entity._async_turn_source_off(COOLING, force=True)

        self.assertEqual(
            services.calls,
            [("climate", "turn_off", {"entity_id": COOLING}, True)],
        )

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

    async def test_heat_cool_exposes_range_and_retains_cooling(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="25",
            fan_state="on",
        )
        entity._heat_cool_enabled = True
        entity._target_temperature_low = 23
        entity._target_temperature_high = 25

        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
        self.assertIn(HVACMode.HEAT_COOL, entity.hvac_modes)
        self.assertTrue(
            entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        )
        entity._async_turn_source_off = AsyncMock()

        await entity._async_reconcile(force=True)

        self.assertEqual(entity._last_active_mode, HVACMode.COOL)
        self.assertFalse(entity._cooling_required)
        self.assertEqual(entity._idle_source_mode, HVACMode.COOL)
        entity._async_turn_source_off.assert_awaited_once_with(COOLING)

    async def test_cooling_idle_turns_source_off_but_keeps_virtual_mode(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="24",
        )
        entity._async_turn_source_off = AsyncMock()

        await entity._async_reconcile(force=True)

        entity._async_turn_source_off.assert_awaited_once_with(COOLING)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)
        self.assertEqual(
            entity.extra_state_attributes[ATTR_IDLE_SOURCE_MODE], HVACMode.COOL
        )

    async def test_heating_idle_turns_zone_off_but_keeps_virtual_mode(self) -> None:
        entity, _services = make_entity(
            heating_state=HVACMode.HEAT,
            sensor_state="24",
        )
        entity._last_active_mode = HVACMode.HEAT
        entity._last_requested_mode = HVACMode.HEAT
        entity._async_turn_source_off = AsyncMock()

        await entity._async_reconcile(force=True)

        entity._async_turn_source_off.assert_awaited_once_with(HEATING)
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)

    async def test_demand_restarts_source_after_idle(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.OFF,
            sensor_state="24.5",
        )
        entity._idle_source_mode = HVACMode.COOL
        entity._async_activate_cooling = AsyncMock()

        await entity._async_reconcile(force=True)

        entity._async_activate_cooling.assert_awaited_once_with(reconcile=False)

    async def test_connected_fan_stays_on_while_source_is_powered_off_idle(
        self,
    ) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.OFF,
            fan_state="on",
        )
        entity._idle_source_mode = HVACMode.COOL
        entity._fan_owned = True

        await entity._async_sync_ceiling_fan()

        self.assertEqual(services.calls, [])

    async def test_heat_cool_switches_after_opposite_boundary(self) -> None:
        entity, _services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="22.7",
        )
        entity._heat_cool_enabled = True
        entity._target_temperature_low = 23
        entity._target_temperature_high = 25
        entity._async_activate_heating = AsyncMock()

        await entity._async_reconcile(force=True)

        entity._async_activate_heating.assert_awaited_once_with(reconcile=False)
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)

    async def test_heat_cool_uses_lower_target_for_heating(self) -> None:
        entity, _services = make_entity(
            heating_state=HVACMode.HEAT,
            sensor_state="23",
        )
        entity._heat_cool_enabled = True
        entity._last_active_mode = HVACMode.HEAT
        entity._target_temperature_low = 23
        entity._target_temperature_high = 25
        entity._async_turn_source_off = AsyncMock()

        await entity._async_reconcile(force=True)

        self.assertFalse(entity._heating_required)
        self.assertEqual(entity._idle_source_mode, HVACMode.HEAT)
        entity._async_turn_source_off.assert_awaited_once_with(HEATING)

    async def test_heat_cool_manual_source_mode_exits_range_mode(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._heat_cool_enabled = True
        event = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.COOL),
                "new_state": climate_state(COOLING, HVACMode.OFF),
            },
        )

        self.assertTrue(entity._sync_mode_from_event(event))
        self.assertFalse(entity._heat_cool_enabled)
        self.assertEqual(entity._last_requested_mode, HVACMode.COOL)

    async def test_delayed_self_mode_event_keeps_heat_cool_enabled(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.OFF)
        entity._heat_cool_enabled = True
        entity._expect_source_mode(COOLING, HVACMode.COOL)
        event = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.OFF),
                "new_state": climate_state(COOLING, HVACMode.COOL),
            },
        )

        self.assertIsNone(entity._sync_mode_from_event(event))
        self.assertTrue(entity._heat_cool_enabled)

    async def test_delayed_source_off_keeps_selected_idle_mode(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._idle_source_mode = HVACMode.COOL
        entity._async_reconcile = AsyncMock()

        await entity._async_sync_source(COOLING)
        entity.hass.states._states[COOLING] = climate_state(COOLING, HVACMode.OFF)
        await entity._async_sync_source(COOLING)

        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)

    async def test_active_source_keeps_mode_marker_for_restart(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)

        await entity._async_activate_cooling_locked()

        self.assertEqual(entity._idle_source_mode, HVACMode.COOL)

    async def test_startup_records_active_source_mode(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._async_reconcile = AsyncMock()

        await entity._async_initialize_control()

        self.assertEqual(entity._idle_source_mode, HVACMode.COOL)

    async def test_delayed_idle_off_keeps_heat_cool_mode(self) -> None:
        entity, _services = make_entity(cooling_state=HVACMode.COOL)
        entity._heat_cool_enabled = True
        entity._idle_source_mode = HVACMode.COOL
        event = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.COOL),
                "new_state": climate_state(COOLING, HVACMode.OFF),
            },
        )

        self.assertIsNone(entity._sync_mode_from_event(event))
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)

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

    async def test_duplicate_source_events_do_not_change_virtual_target(
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
        self.assertFalse(entity._sync_target_from_event(expected))
        self.assertEqual(len(entity._expected_source_temperatures[COOLING]), 1)
        self.assertEqual(entity.target_temperature, 24)

    async def test_self_initiated_mode_change_keeps_virtual_target(self) -> None:
        entity, _services = make_entity()
        event = Event(
            "state_changed",
            {
                "entity_id": COOLING,
                "old_state": climate_state(COOLING, HVACMode.OFF, temperature=None),
                "new_state": climate_state(COOLING, HVACMode.COOL, temperature=25.5),
            },
        )

        async with entity._transition_lock:
            self.assertFalse(entity._sync_target_from_event(event))
        self.assertEqual(entity.target_temperature, 24)

        self.assertTrue(entity._sync_target_from_event(event))
        self.assertEqual(entity.target_temperature, 25.5)

    async def test_ceiling_fan_follows_hvac_mode_and_ignores_idle(self) -> None:
        entity, services = make_entity(fan_state="off", fan_direction="reverse")

        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(
            services.calls,
            [
                (
                    "fan",
                    "set_direction",
                    {"entity_id": FAN, "direction": "forward"},
                    True,
                ),
                ("fan", "turn_on", {"entity_id": FAN}, True),
            ],
        )

        services.calls.clear()
        entity._cooling_required = False

        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(services.calls, [])

    async def test_generic_fan_skips_direction_and_follows_hvac_power(self) -> None:
        entity, services = make_entity(
            fan_state="off",
            fan_supports_direction=False,
        )

        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(
            services.calls,
            [("fan", "turn_on", {"entity_id": FAN}, True)],
        )
        self.assertTrue(entity._fan_owned)

    async def test_variable_speed_fan_follows_cooling_temperature_difference(
        self,
    ) -> None:
        for room_temperature, expected_percentage in (
            ("23", 25),
            ("24.5", 25),
            ("24.6", 50),
            ("27", 100),
        ):
            with self.subTest(room_temperature=room_temperature):
                entity, services = make_entity(
                    sensor_state=room_temperature,
                    fan_state="off",
                    fan_percentage=25,
                )

                await entity._async_sync_ceiling_fan(HVACMode.COOL)

                self.assertEqual(
                    services.calls[-1],
                    (
                        "fan",
                        "turn_on",
                        {"entity_id": FAN, ATTR_PERCENTAGE: expected_percentage},
                        True,
                    ),
                )
                entity._cancel_ceiling_fan_retry()

    async def test_variable_speed_fan_follows_heating_temperature_difference(
        self,
    ) -> None:
        for room_temperature, expected_percentage in (
            ("25", 25),
            ("23.5", 25),
            ("23.4", 50),
            ("20", 100),
        ):
            with self.subTest(room_temperature=room_temperature):
                entity, services = make_entity(
                    sensor_state=room_temperature,
                    fan_state="off",
                    fan_direction="reverse",
                    fan_percentage=25,
                )

                await entity._async_sync_ceiling_fan(HVACMode.HEAT)

                self.assertEqual(
                    services.calls[-1],
                    (
                        "fan",
                        "turn_on",
                        {"entity_id": FAN, ATTR_PERCENTAGE: expected_percentage},
                        True,
                    ),
                )
                entity._cancel_ceiling_fan_retry()

    async def test_variable_speed_fan_only_sends_changed_speed(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="24.5",
            fan_state="on",
            fan_percentage=25,
        )

        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(services.calls, [])

        entity.hass.states._states[SENSOR] = State(SENSOR, "24.7")
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(
            services.calls,
            [
                (
                    "fan",
                    "set_percentage",
                    {"entity_id": FAN, ATTR_PERCENTAGE: 50},
                    True,
                )
            ],
        )

        entity.hass.states._states[FAN] = State(
            FAN,
            "on",
            {
                "direction": "forward",
                ATTR_PERCENTAGE: 50,
                ATTR_PERCENTAGE_STEP: 25,
                ATTR_SUPPORTED_FEATURES: int(
                    FanEntityFeature.DIRECTION | FanEntityFeature.SET_SPEED
                ),
            },
        )
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(len(services.calls), 1)

    async def test_variable_speed_fan_ignores_temperature_boundary_chatter(
        self,
    ) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            sensor_state="24.5",
            fan_state="on",
            fan_percentage=25,
        )

        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        entity.hass.states._states[SENSOR] = State(SENSOR, "24.6")
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(services.calls, [])

        entity.hass.states._states[SENSOR] = State(SENSOR, "24.7")
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(services.calls[-1][2][ATTR_PERCENTAGE], 50)
        entity.hass.states._states[FAN] = State(
            FAN,
            "on",
            {
                "direction": "forward",
                ATTR_PERCENTAGE: 50,
                ATTR_PERCENTAGE_STEP: 25,
                ATTR_SUPPORTED_FEATURES: int(
                    FanEntityFeature.DIRECTION | FanEntityFeature.SET_SPEED
                ),
            },
        )

        entity.hass.states._states[SENSOR] = State(SENSOR, "24.6")
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(len(services.calls), 1)

        entity.hass.states._states[SENSOR] = State(SENSOR, "24.4")
        await entity._async_sync_ceiling_fan(HVACMode.COOL)
        self.assertEqual(services.calls[-1][2][ATTR_PERCENTAGE], 25)

    async def test_ceiling_fan_reverses_for_heat_and_turns_off_with_hvac(
        self,
    ) -> None:
        entity, services = make_entity(fan_state="on", fan_direction="forward")
        entity._fan_owned = True

        await entity._async_sync_ceiling_fan(HVACMode.HEAT)
        entity.hass.states._states[FAN] = State(FAN, "on", {"direction": "reverse"})
        await entity._async_sync_ceiling_fan(HVACMode.OFF)

        self.assertEqual(
            services.calls,
            [
                (
                    "fan",
                    "set_direction",
                    {"entity_id": FAN, "direction": "reverse"},
                    True,
                ),
                ("fan", "turn_off", {"entity_id": FAN}, True),
            ],
        )

    async def test_manually_started_fan_stays_on_when_hvac_is_off(self) -> None:
        entity, services = make_entity(fan_state="on")

        await entity._async_sync_ceiling_fan(HVACMode.OFF)

        self.assertEqual(services.calls, [])

    async def test_running_fan_is_adopted_by_new_hvac_session(self) -> None:
        entity, services = make_entity(fan_state="on")

        await entity._async_sync_ceiling_fan(HVACMode.COOL, adopt=True)
        await entity._async_sync_ceiling_fan(HVACMode.OFF)

        self.assertTrue(entity._ceiling_fan_retry_timer is None)
        self.assertEqual(
            services.calls,
            [("fan", "turn_off", {"entity_id": FAN}, True)],
        )

    async def test_startup_does_not_adopt_running_fan(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            fan_state="on",
        )
        entity._async_reconcile = AsyncMock()

        await entity._async_initialize_control()
        await entity._async_sync_ceiling_fan(HVACMode.OFF)

        self.assertFalse(entity._fan_owned)
        self.assertEqual(services.calls, [])

    async def test_manually_stopped_fan_stays_off_until_next_hvac_session(
        self,
    ) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            fan_state="on",
        )
        entity._fan_owned = True
        old_state = entity.hass.states.get(FAN)
        new_state = State(FAN, "off")
        entity.hass.states._states[FAN] = new_state

        entity._async_source_changed(
            Event(
                "state_changed",
                {
                    "entity_id": FAN,
                    "old_state": old_state,
                    "new_state": new_state,
                },
            )
        )
        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(services.calls, [])
        self.assertFalse(entity._fan_owned)
        self.assertEqual(entity._fan_manual_off_mode, HVACMode.COOL)

        await entity._async_sync_ceiling_fan(HVACMode.OFF)
        await entity._async_sync_ceiling_fan(HVACMode.COOL)

        self.assertEqual(
            services.calls,
            [("fan", "turn_on", {"entity_id": FAN}, True)],
        )

    async def test_ceiling_fan_stays_on_when_source_state_is_unknown(self) -> None:
        entity, services = make_entity(
            cooling_state="unavailable",
            heating_state="off",
            fan_state="on",
        )

        await entity._async_sync_ceiling_fan()

        self.assertEqual(services.calls, [])

    async def test_ceiling_fan_retries_a_mismatch_only_once_per_mode(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            fan_state="off",
            fan_direction="forward",
        )
        tasks = []
        entity.hass.async_create_task = tasks.append

        with patch(
            "custom_components.better_climate.climate.async_call_later",
            return_value=lambda: None,
        ) as call_later:
            await entity._async_sync_ceiling_fan(HVACMode.COOL)

            self.assertEqual(call_later.call_count, 1)
            retry_callback = call_later.call_args.args[2]
            services.calls.clear()
            retry_callback(None)
            await tasks.pop()

            self.assertEqual(
                services.calls,
                [("fan", "turn_on", {"entity_id": FAN}, True)],
            )
            self.assertEqual(call_later.call_count, 1)

    async def test_ceiling_fan_recovery_triggers_the_retry(self) -> None:
        entity, services = make_entity(
            cooling_state=HVACMode.COOL,
            fan_state="off",
            fan_direction="forward",
        )
        entity._ceiling_fan_target = (HVACMode.COOL, None)
        entity._ceiling_fan_retry_used = True
        tasks = []
        entity.hass.async_create_task = tasks.append
        old_state = State(FAN, "unavailable")
        new_state = State(FAN, "off", {"direction": "forward"})
        entity.hass.states._states[FAN] = new_state

        entity._async_source_changed(
            Event(
                "state_changed",
                {
                    "entity_id": FAN,
                    "old_state": old_state,
                    "new_state": new_state,
                },
            )
        )
        await tasks.pop()

        self.assertEqual(
            services.calls,
            [("fan", "turn_on", {"entity_id": FAN}, True)],
        )

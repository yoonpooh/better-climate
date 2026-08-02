"""Climate platform for Better Climate."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from math import ceil, floor, isfinite
from time import monotonic

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_ACTION,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.fan import (
    ATTR_DIRECTION,
    ATTR_PERCENTAGE,
    ATTR_PERCENTAGE_STEP,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    SERVICE_SET_DIRECTION,
    SERVICE_SET_PERCENTAGE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    FanEntityFeature,
)
from homeassistant.components.fan import (
    DOMAIN as FAN_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_COOLING_ENTITY,
    CONF_FAN,
    CONF_FORCE_OFFSET,
    CONF_HEATING_ENTITY,
    CONF_HYSTERESIS,
    CONF_MIN_COMMAND_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
    DOMAIN,
)
from .control import (
    calculate_control,
    calculate_heating_control,
    evaluate_source_target_change,
    round_and_clamp,
    select_heat_cool_mode,
)

EXPECTED_TARGET_TTL = 120
MAX_EXPECTED_TARGETS = 8
FAN_SPEED_STEP = 0.5
FAN_SPEED_HYSTERESIS = 0.1
ATTR_FAN_OWNED = "fan_owned"
ATTR_FAN_MANUAL_OFF_MODE = "fan_manual_off_mode"
ATTR_IDLE_SOURCE_MODE = "idle_source_mode"
ATTR_LAST_REQUESTED_MODE = "last_requested_mode"
DEFAULT_TARGET_LOW = 22
DEFAULT_TARGET_HIGH = 25
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up a Better Climate entity."""
    async_add_entities([BetterClimate(hass, entry)])


class BetterClimate(ClimateEntity, RestoreEntity):
    """Coordinate cooling and optional heating from an external sensor."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the climate entity."""
        self.hass = hass
        self._entry = entry
        self._cooling_entity = entry.data[CONF_COOLING_ENTITY]
        self._heating_entity = entry.data.get(CONF_HEATING_ENTITY)
        self._ceiling_fan_entity = entry.data.get(CONF_FAN)
        self._sensor_entity = entry.data[CONF_TEMPERATURE_SENSOR]
        self._hysteresis = float(entry.data[CONF_HYSTERESIS])
        self._force_offset = float(entry.data[CONF_FORCE_OFFSET])
        self._min_command_interval = float(entry.data[CONF_MIN_COMMAND_INTERVAL])
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer="Better Climate",
        )
        self._target_temperature: float | None = None
        self._target_temperature_low: float | None = None
        self._target_temperature_high: float | None = None
        self._last_active_mode = HVACMode.COOL
        self._last_requested_mode = HVACMode.COOL
        self._heat_cool_enabled = False
        self._idle_source_mode: HVACMode | None = None
        self._cooling_required = False
        self._heating_required = False
        self._last_command_at = 0.0
        self._last_requested_temperature: float | None = None
        self._expected_source_temperatures: dict[str, deque[tuple[float, float]]] = {}
        self._expected_source_modes: dict[str, tuple[HVACMode, float]] = {}
        self._fan_owned = False
        self._fan_manual_off_mode: HVACMode | None = None
        self._ceiling_fan_target: tuple[HVACMode, int | None] | None = None
        self._ceiling_fan_retry_used = False
        self._ceiling_fan_retry_timer: Callable[[], None] | None = None
        self._pending_timer: Callable[[], None] | None = None
        self._control_lock = asyncio.Lock()
        self._transition_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Restore state and start tracking source entities."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            if (
                restored.state == HVACMode.HEAT_COOL
                and self._heating_entity is not None
            ):
                self._heat_cool_enabled = True
                self._last_requested_mode = HVACMode.HEAT_COOL
            restored_mode = restored.attributes.get("last_active_mode")
            if restored_mode not in (HVACMode.COOL, HVACMode.HEAT):
                restored_mode = restored.state
            if restored_mode in (
                HVACMode.COOL,
                HVACMode.HEAT,
            ) and (restored_mode != HVACMode.HEAT or self._heating_entity is not None):
                self._last_active_mode = HVACMode(restored_mode)
            restored_target = restored.attributes.get(ATTR_TEMPERATURE)
            if isinstance(restored_target, (int, float)):
                self._target_temperature = self._normalize_target(
                    float(restored_target)
                )
            restored_low = restored.attributes.get(ATTR_TARGET_TEMP_LOW)
            restored_high = restored.attributes.get(ATTR_TARGET_TEMP_HIGH)
            if isinstance(restored_low, (int, float)):
                self._target_temperature_low = self._normalize_target(
                    float(restored_low)
                )
            if isinstance(restored_high, (int, float)):
                self._target_temperature_high = self._normalize_target(
                    float(restored_high)
                )
            restored_requested_mode = restored.attributes.get(ATTR_LAST_REQUESTED_MODE)
            if restored_requested_mode in (
                HVACMode.COOL,
                HVACMode.HEAT,
                HVACMode.HEAT_COOL,
            ) and (
                restored_requested_mode != HVACMode.HEAT_COOL
                or self._heating_entity is not None
            ):
                self._last_requested_mode = HVACMode(restored_requested_mode)
            restored_idle_mode = restored.attributes.get(ATTR_IDLE_SOURCE_MODE)
            if restored_idle_mode in (HVACMode.COOL, HVACMode.HEAT):
                self._idle_source_mode = HVACMode(restored_idle_mode)
            self._fan_owned = restored.attributes.get(ATTR_FAN_OWNED) is True
            restored_fan_manual_off_mode = restored.attributes.get(
                ATTR_FAN_MANUAL_OFF_MODE
            )
            if restored_fan_manual_off_mode in (HVACMode.COOL, HVACMode.HEAT):
                self._fan_manual_off_mode = HVACMode(restored_fan_manual_off_mode)

        self._ensure_target_temperature()
        self._ensure_target_range()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [
                    entity_id
                    for entity_id in (
                        self._cooling_entity,
                        self._heating_entity,
                        self._sensor_entity,
                        self._ceiling_fan_entity,
                    )
                    if entity_id is not None
                ],
                self._async_source_changed,
            )
        )
        self.async_on_remove(self._cancel_pending_timer)
        self.async_on_remove(self._cancel_ceiling_fan_retry)
        self.hass.async_create_task(self._async_initialize_control())

    @callback
    def _ensure_target_temperature(self) -> None:
        """Initialize the virtual target from the source or room sensor."""
        if self._target_temperature is not None:
            return
        source_target = self._source_attribute(self._cooling_source, ATTR_TEMPERATURE)
        if isinstance(source_target, (int, float)):
            self._target_temperature = self._normalize_target(float(source_target))
            return
        if (room_temperature := self.current_temperature) is None:
            return
        step = self.target_temperature_step
        self._target_temperature = max(
            self.min_temp,
            min(
                self.max_temp,
                floor(room_temperature / step + 0.5) * step,
            ),
        )

    @callback
    def _ensure_target_range(self) -> None:
        """Initialize heat/cool bounds around the single target."""
        if self._heating_entity is None or self._target_temperature is None:
            return
        if self._target_temperature_low is None:
            self._target_temperature_low = self._normalize_target(DEFAULT_TARGET_LOW)
        if self._target_temperature_high is None:
            self._target_temperature_high = self._normalize_target(DEFAULT_TARGET_HIGH)
        if self._target_temperature_low >= self._target_temperature_high:
            step = self.target_temperature_step
            if self._target_temperature_high < self.max_temp:
                self._target_temperature_high = self._normalize_target(
                    self._target_temperature_high + step
                )
            else:
                self._target_temperature_low = self._normalize_target(
                    self._target_temperature_low - step
                )

    @property
    def _cooling_source(self) -> State | None:
        return self.hass.states.get(self._cooling_entity)

    @property
    def _heating_source(self) -> State | None:
        if self._heating_entity is None:
            return None
        return self.hass.states.get(self._heating_entity)

    @property
    def _source_sensor(self) -> State | None:
        return self.hass.states.get(self._sensor_entity)

    @property
    def available(self) -> bool:
        """Return whether the sensor and active source are available."""
        if not self._is_available(self._source_sensor):
            return False
        if self.hvac_mode == HVACMode.HEAT:
            return self._is_available(self._heating_source)
        if self.hvac_mode == HVACMode.COOL:
            return self._is_available(self._cooling_source)
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return self._is_available(self._cooling_source) and self._is_available(
                self._heating_source
            )
        return self._is_available(self._cooling_source) or self._is_available(
            self._heating_source
        )

    @staticmethod
    def _is_available(state: State | None) -> bool:
        return state is not None and state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )

    @property
    def current_temperature(self) -> float | None:
        """Return the external room temperature."""
        state = self._source_sensor
        try:
            value = float(state.state) if state is not None else None
        except (TypeError, ValueError):
            return None
        return value if value is not None and isfinite(value) else None

    @property
    def target_temperature(self) -> float | None:
        """Return the virtual target temperature."""
        return self._target_temperature

    @property
    def target_temperature_low(self) -> float | None:
        """Return the automatic heating boundary."""
        return self._target_temperature_low

    @property
    def target_temperature_high(self) -> float | None:
        """Return the automatic cooling boundary."""
        return self._target_temperature_high

    @property
    def temperature_unit(self) -> str:
        """Return Home Assistant's configured temperature unit."""
        return self.hass.config.units.temperature_unit

    @property
    def min_temp(self) -> float:
        """Return the common minimum target."""
        minimum = self._source_min_temp(self._cooling_source)
        if self._heating_source is not None:
            minimum = max(minimum, self._source_min_temp(self._heating_source))
        return minimum

    @property
    def max_temp(self) -> float:
        """Return the common maximum target."""
        maximum = self._source_max_temp(self._cooling_source)
        if self._heating_source is not None:
            maximum = min(maximum, self._source_max_temp(self._heating_source))
        return maximum

    @property
    def target_temperature_step(self) -> float:
        """Return a target step supported by both sources."""
        step = self._source_temperature_step(self._cooling_source)
        if self._heating_source is not None:
            step = max(step, self._source_temperature_step(self._heating_source))
        return step

    def _source_min_temp(self, state: State | None) -> float:
        return self._source_float_attribute(state, ATTR_MIN_TEMP, 16)

    def _source_max_temp(self, state: State | None) -> float:
        return self._source_float_attribute(state, ATTR_MAX_TEMP, 30)

    def _source_temperature_step(self, state: State | None) -> float:
        step = self._source_float_attribute(state, ATTR_TARGET_TEMP_STEP, 0.5)
        return step if step > 0 else 0.5

    def _source_float_attribute(
        self, state: State | None, name: str, default: float
    ) -> float:
        """Return a finite numeric source capability."""
        try:
            value = float(self._source_attribute(state, name, default))
        except (TypeError, ValueError):
            return default
        return value if isfinite(value) else default

    def _normalize_target(self, value: float) -> float:
        """Normalize a virtual target to the shared source capabilities."""
        return round_and_clamp(
            value,
            minimum=self.min_temp,
            maximum=self.max_temp,
            step=self.target_temperature_step,
        )

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the active source mode."""
        if self._heat_cool_enabled:
            return HVACMode.HEAT_COOL
        return self._source_hvac_mode() or self._idle_source_mode or HVACMode.OFF

    def _source_hvac_mode(self) -> HVACMode | None:
        """Return the concrete active source mode."""
        cooling_active = (
            self._cooling_source is not None
            and self._cooling_source.state == HVACMode.COOL
        )
        heating_active = (
            self._heating_source is not None
            and self._heating_source.state == HVACMode.HEAT
        )
        if cooling_active and heating_active:
            return self._last_active_mode
        if cooling_active:
            return HVACMode.COOL
        if heating_active:
            return HVACMode.HEAT
        return None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return supported HVAC modes."""
        modes = [HVACMode.OFF, HVACMode.COOL]
        if self._heating_entity is not None:
            modes.extend((HVACMode.HEAT, HVACMode.HEAT_COOL))
        return modes

    @property
    def hvac_action(self) -> HVACAction:
        """Return the active source action."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        active_mode = self._source_hvac_mode()
        if active_mode is None:
            return HVACAction.IDLE
        source = (
            self._cooling_source
            if active_mode == HVACMode.COOL
            else self._heating_source
        )
        action = source.attributes.get(ATTR_HVAC_ACTION) if source else None
        if isinstance(action, str):
            try:
                return HVACAction(action)
            except ValueError:
                pass
        if active_mode == HVACMode.COOL and self._cooling_required:
            return HVACAction.COOLING
        if active_mode == HVACMode.HEAT and self._heating_required:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Expose only the controls this wrapper owns."""
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._heating_entity is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return features

    @property
    def extra_state_attributes(self) -> dict:
        """Return diagnostic control state."""
        return {
            "cooling_source": self._cooling_entity,
            "heating_source": self._heating_entity,
            "fan": self._ceiling_fan_entity,
            ATTR_FAN_OWNED: self._fan_owned,
            ATTR_FAN_MANUAL_OFF_MODE: self._fan_manual_off_mode,
            "temperature_sensor": self._sensor_entity,
            "cooling_source_hvac_mode": self._state_value(self._cooling_source),
            "heating_source_hvac_mode": self._state_value(self._heating_source),
            "last_active_mode": self._last_active_mode,
            ATTR_LAST_REQUESTED_MODE: self._last_requested_mode,
            ATTR_IDLE_SOURCE_MODE: self._idle_source_mode,
            "cooling_required": self._cooling_required,
            "heating_required": self._heating_required,
            "command_temperature": self._active_source_temperature,
            "last_requested_temperature": (self._last_requested_temperature),
        }

    @staticmethod
    def _state_value(state: State | None) -> str | None:
        return state.state if state is not None else None

    @property
    def _active_source_temperature(self) -> float | None:
        active_mode = self._source_hvac_mode()
        if active_mode == HVACMode.COOL:
            source = self._cooling_source
        elif active_mode == HVACMode.HEAT:
            source = self._heating_source
        else:
            return None
        value = self._source_attribute(source, ATTR_TEMPERATURE)
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _source_attribute(state: State | None, name: str, default=None):
        if state is None:
            return default
        return state.attributes.get(name, default)

    async def async_set_temperature(self, **kwargs) -> None:
        """Set the virtual room target."""
        target_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        target_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if target_low is not None or target_high is not None:
            if self._heating_entity is None:
                raise HomeAssistantError("Heating source is not configured")
            self._ensure_target_temperature()
            self._ensure_target_range()
            current_low = self._target_temperature_low
            current_high = self._target_temperature_high
            if current_low is None or current_high is None:
                raise HomeAssistantError("Target temperature range is unavailable")
            try:
                low = self._normalize_target(
                    float(target_low if target_low is not None else current_low)
                )
                high = self._normalize_target(
                    float(target_high if target_high is not None else current_high)
                )
            except (OverflowError, TypeError, ValueError) as err:
                raise HomeAssistantError("Invalid target temperature range") from err
            if low > high:
                raise HomeAssistantError(
                    "Heating target must not exceed cooling target"
                )
            self._target_temperature_low = low
            self._target_temperature_high = high
            self.async_write_ha_state()
            if self.hvac_mode == HVACMode.HEAT_COOL:
                await self._async_reconcile(force=True)
            return
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        try:
            self._target_temperature = self._normalize_target(float(temperature))
        except (OverflowError, TypeError, ValueError) as err:
            raise HomeAssistantError("Invalid target temperature") from err
        self.async_write_ha_state()
        if self.hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
            await self._async_reconcile(force=True)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Select the cooling source, heating source, or off."""
        if hvac_mode == HVACMode.COOL:
            self._heat_cool_enabled = False
            self._last_requested_mode = HVACMode.COOL
            await self._async_activate_cooling()
            return
        if hvac_mode == HVACMode.HEAT and self._heating_entity is not None:
            self._heat_cool_enabled = False
            self._last_requested_mode = HVACMode.HEAT
            await self._async_activate_heating()
            return
        if hvac_mode == HVACMode.HEAT_COOL and self._heating_entity is not None:
            self._ensure_target_range()
            self._heat_cool_enabled = True
            self._last_requested_mode = HVACMode.HEAT_COOL
            self.async_write_ha_state()
            await self._async_reconcile(force=True)
            return
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")

    async def async_turn_on(self) -> None:
        """Resume the most recently active mode."""
        if (
            self._last_requested_mode == HVACMode.HEAT_COOL
            and self._heating_entity is not None
        ):
            await self.async_set_hvac_mode(HVACMode.HEAT_COOL)
            return
        if self._last_active_mode == HVACMode.HEAT and self._heating_entity is not None:
            await self._async_activate_heating()
            return
        await self._async_activate_cooling()

    async def _async_activate_cooling(self, *, reconcile: bool = True) -> None:
        """Turn off heating before enabling cooling."""
        async with self._transition_lock:
            await self._async_activate_cooling_locked()
        if reconcile:
            await self._async_reconcile(force=True)

    async def _async_activate_cooling_locked(self) -> None:
        """Enable cooling while holding the transition lock."""
        if not self._is_available(self._cooling_source):
            raise HomeAssistantError("Cooling source is unavailable")
        self._last_active_mode = HVACMode.COOL
        self._cancel_pending_timer()
        self._heating_required = False
        if self._heating_entity is not None:
            await self._async_turn_source_off(self._heating_entity)
        await self._async_set_source_mode(self._cooling_entity, HVACMode.COOL)
        self._idle_source_mode = None
        await self._async_sync_ceiling_fan(HVACMode.COOL, adopt=True)
        self.async_write_ha_state()

    async def _async_activate_heating(self, *, reconcile: bool = True) -> None:
        """Turn off cooling before enabling heating."""
        async with self._transition_lock:
            await self._async_activate_heating_locked()
        if reconcile:
            await self._async_reconcile(force=True)

    async def _async_activate_heating_locked(self) -> None:
        """Enable heating while holding the transition lock."""
        if self._heating_entity is None:
            raise HomeAssistantError("Heating source is not configured")
        if not self._is_available(self._heating_source):
            raise HomeAssistantError("Heating source is unavailable")
        self._last_active_mode = HVACMode.HEAT
        self._cancel_pending_timer()
        self._cooling_required = False
        await self._async_turn_source_off(self._cooling_entity)
        await self._async_set_source_mode(self._heating_entity, HVACMode.HEAT)
        self._idle_source_mode = None
        await self._async_sync_ceiling_fan(HVACMode.HEAT, adopt=True)
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off every configured source."""
        self._heat_cool_enabled = False
        self._idle_source_mode = None
        async with self._transition_lock:
            await self._async_turn_off_locked()

    async def _async_turn_off_locked(self) -> None:
        """Turn off every source while holding the transition lock."""
        self._cancel_pending_timer()
        self._cooling_required = False
        self._heating_required = False
        entities = [self._cooling_entity]
        if self._heating_entity is not None:
            entities.append(self._heating_entity)
        active_entity = (
            self._heating_entity
            if self._source_hvac_mode() == HVACMode.HEAT
            else self._cooling_entity
        )
        entities.sort(key=lambda entity_id: entity_id != active_entity)

        errors = []
        for entity_id in entities:
            try:
                await self._async_turn_source_off(entity_id)
            except Exception as err:  # noqa: BLE001
                # Continue so one failed source cannot leave the other running.
                errors.append(f"{entity_id}: {err}")
        await self._async_sync_ceiling_fan()
        self.async_write_ha_state()
        if errors:
            raise HomeAssistantError(
                "Failed to turn off all climate sources: " + "; ".join(errors)
            )

    async def _async_turn_source_off(self, entity_id: str) -> None:
        """Turn off a source and confirm it is off before continuing."""
        state = self.hass.states.get(entity_id)
        if state is not None and state.state == HVACMode.OFF:
            return

        loop = asyncio.get_running_loop()
        stopped = loop.create_future()

        @callback
        def _source_changed(
            event: Event[EventStateChangedData],
        ) -> None:
            new_state = event.data["new_state"]
            if (
                new_state is not None
                and new_state.state == HVACMode.OFF
                and not stopped.done()
            ):
                stopped.set_result(None)

        unsubscribe = async_track_state_change_event(
            self.hass, [entity_id], _source_changed
        )
        try:
            await self._async_set_source_mode(entity_id, HVACMode.OFF)
            state = self.hass.states.get(entity_id)
            if state is None or state.state != HVACMode.OFF:
                try:
                    await asyncio.wait_for(stopped, timeout=10)
                except TimeoutError as err:
                    raise HomeAssistantError(f"{entity_id} did not turn off") from err
        finally:
            unsubscribe()

    async def _async_set_source_temperature(
        self, entity_id: str, temperature: float
    ) -> None:
        """Set and record a requested source temperature."""
        pending = self._expected_source_temperatures.setdefault(
            entity_id, deque(maxlen=MAX_EXPECTED_TARGETS)
        )
        expected = (temperature, monotonic())
        pending.append(expected)
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": entity_id,
                    ATTR_TEMPERATURE: temperature,
                },
                blocking=True,
            )
        except Exception:
            try:
                pending.remove(expected)
            except ValueError:
                pass
            if not pending:
                self._expected_source_temperatures.pop(entity_id, None)
            raise
        self._last_requested_temperature = temperature

    @callback
    def _expected_targets(self, entity_id: str) -> list[float]:
        """Return non-expired source commands."""
        pending = self._expected_source_temperatures.get(entity_id)
        if pending is None:
            return []
        now = monotonic()
        while pending and now - pending[0][1] > EXPECTED_TARGET_TTL:
            pending.popleft()
        if not pending:
            self._expected_source_temperatures.pop(entity_id, None)
            return []
        return [temperature for temperature, _created_at in pending]

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self._ceiling_fan_entity:
            old_state = event.data["old_state"]
            new_state = event.data["new_state"]
            mode = self._ceiling_fan_hvac_mode()
            if (
                old_state is not None
                and old_state.state == STATE_ON
                and new_state is not None
                and new_state.state == STATE_OFF
                and mode in (HVACMode.COOL, HVACMode.HEAT)
            ):
                self._fan_manual_off_mode = mode
                self._fan_owned = False
                self._cancel_ceiling_fan_retry()
                self.async_write_ha_state()
                return
            if new_state is not None and new_state.state == STATE_ON:
                self._fan_manual_off_mode = None
                self.async_write_ha_state()
            if not self._is_available(old_state) and self._is_available(new_state):
                self._cancel_ceiling_fan_retry()
                self._ceiling_fan_retry_used = False
                self.hass.async_create_task(self._async_sync_ceiling_fan())
            return
        mode_changed = self._sync_mode_from_event(event)
        target_changed = (
            False if mode_changed is None else self._sync_target_from_event(event)
        )
        self.async_write_ha_state()
        if entity_id in (self._cooling_entity, self._heating_entity):
            self.hass.async_create_task(
                self._async_sync_source(
                    entity_id, force=mode_changed is True or target_changed
                )
            )
        else:
            self._schedule_reconcile()

    @callback
    def _sync_mode_from_event(self, event: Event[EventStateChangedData]) -> bool | None:
        """Leave heat/cool for manual mode changes; mark our own events as None."""
        entity_id = event.data["entity_id"]
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if (
            entity_id not in (self._cooling_entity, self._heating_entity)
            or old_state is None
            or new_state is None
            or old_state.state == new_state.state
        ):
            return False
        if self._consume_expected_source_mode(entity_id, new_state.state):
            return None
        if not self._heat_cool_enabled or self._transition_lock.locked():
            return False

        self._heat_cool_enabled = False
        mode = HVACMode.COOL if entity_id == self._cooling_entity else HVACMode.HEAT
        self._last_requested_mode = mode
        if new_state.state == mode:
            self._last_active_mode = mode
        return True

    def _expect_source_mode(self, entity_id: str, mode: HVACMode) -> None:
        """Remember a source mode command until its delayed state event arrives."""
        self._expected_source_modes[entity_id] = (mode, monotonic())

    async def _async_set_source_mode(self, entity_id: str, mode: HVACMode) -> None:
        """Set and record a requested source mode."""
        self._expect_source_mode(entity_id, mode)
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": mode},
                blocking=True,
            )
        except Exception:
            self._expected_source_modes.pop(entity_id, None)
            raise

    def _consume_expected_source_mode(self, entity_id: str, mode: str) -> bool:
        """Return whether a source mode event matches our latest command."""
        expected = self._expected_source_modes.get(entity_id)
        if expected is None:
            return False
        expected_mode, created_at = expected
        if monotonic() - created_at > EXPECTED_TARGET_TTL:
            self._expected_source_modes.pop(entity_id, None)
            return False
        if mode != expected_mode:
            return False
        self._expected_source_modes.pop(entity_id, None)
        return True

    @callback
    def _sync_target_from_event(self, event: Event[EventStateChangedData]) -> bool:
        """Adopt target changes made through an original source entity."""
        entity_id = event.data["entity_id"]
        if entity_id not in (self._cooling_entity, self._heating_entity):
            return False

        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if old_state is None or new_state is None:
            return False
        if self._transition_lock.locked() and old_state.state != new_state.state:
            return False
        old_target = old_state.attributes.get(ATTR_TEMPERATURE)
        new_target = new_state.attributes.get(ATTR_TEMPERATURE)
        if not isinstance(new_target, (int, float)):
            return False
        new_target = float(new_target)
        if not isfinite(new_target):
            return False

        step = self._source_temperature_step(new_state)
        expected_targets = self._expected_targets(entity_id)
        adopt, _ = evaluate_source_target_change(
            old_target=(
                float(old_target) if isinstance(old_target, (int, float)) else None
            ),
            new_target=new_target,
            expected_targets=expected_targets,
            step=step,
        )
        if not adopt:
            return False

        self._expected_source_temperatures.pop(entity_id, None)
        normalized_target = self._normalize_target(new_target)
        if self._heat_cool_enabled:
            if entity_id == self._cooling_entity:
                self._target_temperature_high = max(
                    normalized_target,
                    self._target_temperature_low or normalized_target,
                )
            else:
                self._target_temperature_low = min(
                    normalized_target,
                    self._target_temperature_high or normalized_target,
                )
        else:
            self._target_temperature = normalized_target
        return True

    async def _async_sync_source(self, entity_id: str, *, force: bool = False) -> None:
        """Mirror original source changes and enforce mutual exclusion."""
        async with self._transition_lock:
            state = self.hass.states.get(entity_id)
            if (
                entity_id == self._cooling_entity
                and state is not None
                and state.state == HVACMode.COOL
            ):
                self._idle_source_mode = None
                self._last_active_mode = HVACMode.COOL
                self._cancel_pending_timer()
                self._heating_required = False
                if self._heating_entity is not None:
                    await self._async_turn_source_off(self._heating_entity)
            elif (
                entity_id == self._heating_entity
                and state is not None
                and state.state == HVACMode.HEAT
            ):
                self._idle_source_mode = None
                self._last_active_mode = HVACMode.HEAT
                self._cancel_pending_timer()
                self._cooling_required = False
                await self._async_turn_source_off(self._cooling_entity)
            elif entity_id == self._cooling_entity:
                self._cooling_required = False
            elif entity_id == self._heating_entity:
                self._heating_required = False

            await self._async_sync_ceiling_fan()
            self.async_write_ha_state()
        await self._async_reconcile(force=force)

    async def _async_initialize_control(self) -> None:
        """Enforce a single active source after Home Assistant starts."""
        async with self._transition_lock:
            self._ensure_target_temperature()
            cooling_active = (
                self._cooling_source is not None
                and self._cooling_source.state == HVACMode.COOL
            )
            heating_active = (
                self._heating_source is not None
                and self._heating_source.state == HVACMode.HEAT
            )
            if cooling_active and heating_active:
                if self._last_active_mode == HVACMode.HEAT:
                    await self._async_turn_source_off(self._cooling_entity)
                elif self._heating_entity is not None:
                    await self._async_turn_source_off(self._heating_entity)
            elif cooling_active:
                self._last_active_mode = HVACMode.COOL
            elif heating_active:
                self._last_active_mode = HVACMode.HEAT
            await self._async_sync_ceiling_fan()
            self.async_write_ha_state()
        await self._async_reconcile(force=True)

    @callback
    def _schedule_reconcile(self, force: bool = False) -> None:
        self.hass.async_create_task(self._async_reconcile(force=force))

    async def _async_reconcile(self, force: bool = False) -> None:
        virtual_mode = self.hvac_mode
        selected_mode = self._source_hvac_mode() or self._idle_source_mode
        if virtual_mode == HVACMode.HEAT_COOL:
            if (
                self._target_temperature_low is None
                or self._target_temperature_high is None
                or self.current_temperature is None
            ):
                return
            selected_mode = HVACMode(
                select_heat_cool_mode(
                    room_temperature=self.current_temperature,
                    target_low=self._target_temperature_low,
                    target_high=self._target_temperature_high,
                    hysteresis=self._hysteresis,
                    active_mode=selected_mode,
                    last_active_mode=self._last_active_mode,
                )
            )
        async with self._control_lock:
            mode = selected_mode if virtual_mode == HVACMode.HEAT_COOL else virtual_mode
            if (
                mode not in (HVACMode.COOL, HVACMode.HEAT)
                or self._target_temperature is None
            ):
                return

            source = (
                self._cooling_source if mode == HVACMode.COOL else self._heating_source
            )
            source_entity = (
                self._cooling_entity if mode == HVACMode.COOL else self._heating_entity
            )
            if (
                source is None
                or source_entity is None
                or not self._is_available(source)
            ):
                return
            if self.min_temp > self.max_temp:
                return

            target_temperature = self._control_target_for_mode(mode)
            if target_temperature is None:
                return
            normalized_target = self._normalize_target(target_temperature)
            if normalized_target != target_temperature:
                if virtual_mode == HVACMode.HEAT_COOL:
                    if mode == HVACMode.COOL:
                        self._target_temperature_high = normalized_target
                    else:
                        self._target_temperature_low = normalized_target
                else:
                    self._target_temperature = normalized_target
                self.async_write_ha_state()

            internal = source.attributes.get("current_temperature")
            room = self.current_temperature
            if (
                not isinstance(internal, (int, float))
                or not isfinite(float(internal))
                or room is None
            ):
                await self._async_fallback_to_source_thermostat(mode, source)
                return

            if mode == HVACMode.COOL:
                cooling_result = calculate_control(
                    room_temperature=room,
                    target_temperature=normalized_target,
                    internal_temperature=float(internal),
                    cooling_required=self._cooling_required,
                    hysteresis=self._hysteresis,
                    force_offset=self._force_offset,
                    minimum=self._source_min_temp(source),
                    maximum=self._source_max_temp(source),
                    step=self._source_temperature_step(source),
                )
                self._cooling_required = cooling_result.cooling_required
                command_temperature = cooling_result.command_temperature
            else:
                heating_result = calculate_heating_control(
                    room_temperature=room,
                    target_temperature=normalized_target,
                    internal_temperature=float(internal),
                    heating_required=self._heating_required,
                    hysteresis=self._hysteresis,
                    force_offset=self._force_offset,
                    minimum=self._source_min_temp(source),
                    maximum=self._source_max_temp(source),
                    step=self._source_temperature_step(source),
                )
                self._heating_required = heating_result.heating_required
                command_temperature = heating_result.command_temperature
            self.async_write_ha_state()
            await self._async_sync_ceiling_fan(mode)

            conditioning_required = (
                self._cooling_required
                if mode == HVACMode.COOL
                else self._heating_required
            )
            if not conditioning_required:
                self._idle_source_mode = mode
                if source.state != HVACMode.OFF:
                    await self._async_turn_source_off(source_entity)
                self.async_write_ha_state()
                return

            if source.state != mode:
                if mode == HVACMode.COOL:
                    await self._async_activate_cooling(reconcile=False)
                else:
                    await self._async_activate_heating(reconcile=False)
                self._heat_cool_enabled = virtual_mode == HVACMode.HEAT_COOL
                if self._heat_cool_enabled:
                    self._last_requested_mode = HVACMode.HEAT_COOL
                self._idle_source_mode = None
                self.async_write_ha_state()

            current_command = source.attributes.get(ATTR_TEMPERATURE)
            source_step = self._source_temperature_step(source)
            if (
                isinstance(current_command, (int, float))
                and abs(float(current_command) - command_temperature) < source_step / 2
            ):
                return

            elapsed = monotonic() - self._last_command_at
            if not force and elapsed < self._min_command_interval:
                self._set_pending_timer(self._min_command_interval - elapsed)
                return

            self._cancel_pending_timer()
            await self._async_set_source_temperature(
                source_entity,
                command_temperature,
            )
            self._last_command_at = monotonic()
            self.async_write_ha_state()

    def _control_target_for_mode(self, mode: HVACMode) -> float | None:
        """Return the target used by the concrete source."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return (
                self._target_temperature_high
                if mode == HVACMode.COOL
                else self._target_temperature_low
            )
        return self._target_temperature

    async def _async_fallback_to_source_thermostat(
        self, mode: HVACMode, source: State
    ) -> None:
        """Remove forced setpoints when external control data is unavailable."""
        target = self._control_target_for_mode(mode)
        if target is None:
            return
        self._cancel_pending_timer()
        self._cooling_required = False
        self._heating_required = False
        if source.state == HVACMode.OFF and self._idle_source_mode == mode:
            if mode == HVACMode.COOL:
                await self._async_activate_cooling(reconcile=False)
            else:
                await self._async_activate_heating(reconcile=False)
        fallback = round_and_clamp(
            target,
            minimum=self._source_min_temp(source),
            maximum=self._source_max_temp(source),
            step=self._source_temperature_step(source),
        )
        current = source.attributes.get(ATTR_TEMPERATURE)
        if (
            isinstance(current, (int, float))
            and abs(float(current) - fallback)
            < self._source_temperature_step(source) / 2
        ):
            self.async_write_ha_state()
            return
        source_entity = (
            self._cooling_entity if mode == HVACMode.COOL else self._heating_entity
        )
        if source_entity is None:
            return
        await self._async_set_source_temperature(source_entity, fallback)
        self._last_command_at = monotonic()
        self.async_write_ha_state()

    @callback
    def _set_pending_timer(self, delay: float) -> None:
        if self._pending_timer is not None:
            return

        @callback
        def _run(_now) -> None:
            self._pending_timer = None
            self._schedule_reconcile()

        self._pending_timer = async_call_later(self.hass, delay, _run)

    @callback
    def _cancel_pending_timer(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer()
            self._pending_timer = None

    async def _async_sync_ceiling_fan(
        self,
        mode: HVACMode | None = None,
        *,
        retry: bool = False,
        adopt: bool = False,
    ) -> None:
        """Match the optional ceiling fan to the active HVAC mode."""
        if self._ceiling_fan_entity is None:
            return
        if mode is None:
            mode = self._ceiling_fan_hvac_mode()
        if mode is None:
            return
        if mode == HVACMode.OFF:
            self._fan_manual_off_mode = None
        elif self._fan_manual_off_mode == mode:
            self._cancel_ceiling_fan_retry()
            return
        else:
            self._fan_manual_off_mode = None

        fan = self.hass.states.get(self._ceiling_fan_entity)
        percentage = self._ceiling_fan_percentage(mode, fan)
        target = (mode, percentage)
        if retry:
            self._cancel_ceiling_fan_retry()
            if (
                target != self._ceiling_fan_target
                or self._ceiling_fan_hvac_mode() != mode
            ):
                return
            self._ceiling_fan_retry_used = True
        elif target != self._ceiling_fan_target:
            self._cancel_ceiling_fan_retry()
            self._ceiling_fan_target = target
            self._ceiling_fan_retry_used = False

        if mode == HVACMode.OFF and not self._fan_owned:
            self._cancel_ceiling_fan_retry()
            return

        if fan is None or not self._is_available(fan):
            if not retry:
                self._set_ceiling_fan_retry(mode)
            return
        if adopt and mode in (HVACMode.COOL, HVACMode.HEAT) and fan.state == STATE_ON:
            self._fan_owned = True

        direction = (
            DIRECTION_FORWARD
            if mode == HVACMode.COOL
            else DIRECTION_REVERSE
            if mode == HVACMode.HEAT
            else None
        )
        if not (
            FanEntityFeature(fan.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
            & FanEntityFeature.DIRECTION
        ):
            direction = None
        percentage_matches = percentage is None or self._fan_percentage_matches(
            fan, percentage
        )
        matches = (
            fan.state == STATE_OFF
            if mode == HVACMode.OFF
            else fan.state == STATE_ON
            and (direction is None or fan.attributes.get(ATTR_DIRECTION) == direction)
            and percentage_matches
        )
        if matches:
            if mode == HVACMode.OFF:
                self._fan_owned = False
            self._cancel_ceiling_fan_retry()
            return
        if not retry and (
            self._ceiling_fan_retry_used or self._ceiling_fan_retry_timer is not None
        ):
            return

        try:
            if mode == HVACMode.OFF:
                if fan.state != STATE_OFF:
                    await self.hass.services.async_call(
                        FAN_DOMAIN,
                        SERVICE_TURN_OFF,
                        {"entity_id": self._ceiling_fan_entity},
                        blocking=True,
                    )
                self._fan_owned = False
                return
            if mode not in (HVACMode.COOL, HVACMode.HEAT):
                return

            if (
                direction is not None
                and fan.attributes.get(ATTR_DIRECTION) != direction
            ):
                await self.hass.services.async_call(
                    FAN_DOMAIN,
                    SERVICE_SET_DIRECTION,
                    {
                        "entity_id": self._ceiling_fan_entity,
                        ATTR_DIRECTION: direction,
                    },
                    blocking=True,
                )
            if fan.state != STATE_ON:
                data = {"entity_id": self._ceiling_fan_entity}
                if percentage is not None:
                    data[ATTR_PERCENTAGE] = percentage
                await self.hass.services.async_call(
                    FAN_DOMAIN,
                    SERVICE_TURN_ON,
                    data,
                    blocking=True,
                )
                self._fan_owned = True
            elif not percentage_matches:
                await self.hass.services.async_call(
                    FAN_DOMAIN,
                    SERVICE_SET_PERCENTAGE,
                    {
                        "entity_id": self._ceiling_fan_entity,
                        ATTR_PERCENTAGE: percentage,
                    },
                    blocking=True,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to synchronize ceiling fan %s: %s",
                self._ceiling_fan_entity,
                err,
            )
        if not retry:
            self._set_ceiling_fan_retry(mode)

    def _ceiling_fan_percentage(self, mode: HVACMode, fan: State | None) -> int | None:
        """Return the fan speed for the current room temperature difference."""
        room_temperature = self.current_temperature
        target_temperature = self._control_target_for_mode(mode)
        if (
            fan is None
            or mode not in (HVACMode.COOL, HVACMode.HEAT)
            or room_temperature is None
            or target_temperature is None
            or not (
                FanEntityFeature(fan.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
                & FanEntityFeature.SET_SPEED
            )
        ):
            return None
        try:
            percentage_step = float(fan.attributes[ATTR_PERCENTAGE_STEP])
        except (KeyError, TypeError, ValueError):
            return None
        if not isfinite(percentage_step) or not 0 < percentage_step <= 100:
            return None

        difference = round(
            room_temperature - target_temperature
            if mode == HVACMode.COOL
            else target_temperature - room_temperature,
            3,
        )
        speed_count = max(1, round(100 / percentage_step))
        level = min(
            1 if difference <= FAN_SPEED_STEP else ceil(difference / FAN_SPEED_STEP),
            speed_count,
        )
        previous = self._ceiling_fan_target
        if previous is not None and previous[0] == mode and previous[1] is not None:
            previous_level = round(previous[1] * speed_count / 100)
            if 1 <= previous_level <= speed_count and (
                (
                    level > previous_level
                    and difference
                    <= previous_level * FAN_SPEED_STEP + FAN_SPEED_HYSTERESIS
                )
                or (
                    level < previous_level
                    and difference
                    > (previous_level - 1) * FAN_SPEED_STEP - FAN_SPEED_HYSTERESIS
                )
            ):
                level = previous_level
        return round(level * 100 / speed_count)

    @staticmethod
    def _fan_percentage_matches(fan: State, percentage: int) -> bool:
        current = fan.attributes.get(ATTR_PERCENTAGE)
        step = fan.attributes.get(ATTR_PERCENTAGE_STEP)
        return (
            isinstance(current, (int, float))
            and isinstance(step, (int, float))
            and abs(float(current) - percentage) < float(step) / 2
        )

    def _ceiling_fan_hvac_mode(self) -> HVACMode | None:
        """Return an explicit active or fully-off mode, preserving unknown states."""
        cooling = self._cooling_source
        heating = self._heating_source
        cooling_active = cooling is not None and cooling.state == HVACMode.COOL
        heating_active = heating is not None and heating.state == HVACMode.HEAT
        if cooling_active and heating_active:
            return self._last_active_mode
        if cooling_active:
            return HVACMode.COOL
        if heating_active:
            return HVACMode.HEAT
        sources = [cooling]
        if self._heating_entity is not None:
            sources.append(heating)
        if all(
            source is not None and source.state == HVACMode.OFF for source in sources
        ):
            if self._idle_source_mode in (HVACMode.COOL, HVACMode.HEAT):
                return self._idle_source_mode
            return HVACMode.OFF
        return None

    @callback
    def _set_ceiling_fan_retry(self, mode: HVACMode) -> None:
        """Schedule one verification and retry for a fan command."""
        if self._ceiling_fan_retry_timer is not None or self._ceiling_fan_retry_used:
            return

        @callback
        def _run(_now) -> None:
            self._ceiling_fan_retry_timer = None
            self.hass.async_create_task(self._async_sync_ceiling_fan(mode, retry=True))

        self._ceiling_fan_retry_timer = async_call_later(
            self.hass, self._min_command_interval, _run
        )

    @callback
    def _cancel_ceiling_fan_retry(self) -> None:
        if self._ceiling_fan_retry_timer is not None:
            self._ceiling_fan_retry_timer()
            self._ceiling_fan_retry_timer = None

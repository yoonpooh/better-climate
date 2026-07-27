"""Config flow for Better Climate."""

from collections.abc import Mapping
from math import isfinite
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    HVACMode,
)
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.components.fan import FanEntityFeature
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import ATTR_SUPPORTED_FEATURES, CONF_NAME
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_CEILING_FAN,
    CONF_COOLING_ENTITY,
    CONF_FORCE_OFFSET,
    CONF_HEATING_ENTITY,
    CONF_HYSTERESIS,
    CONF_MIN_COMMAND_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_FORCE_OFFSET,
    DEFAULT_HYSTERESIS,
    DEFAULT_MIN_COMMAND_INTERVAL,
    DOMAIN,
)


class BetterClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Better Climate config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Create a Better Climate entity."""
        errors = {}
        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_COOLING_ENTITY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Update an existing configuration."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_COOLING_ENTITY])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(user_input or entry.data),
            errors=errors,
        )

    def _validate(self, data: dict[str, Any]) -> dict[str, str]:
        errors = {}
        cooling = self.hass.states.get(data[CONF_COOLING_ENTITY])
        if cooling is None:
            errors[CONF_COOLING_ENTITY] = "entity_not_found"
        elif HVACMode.COOL not in cooling.attributes.get(ATTR_HVAC_MODES, []):
            errors[CONF_COOLING_ENTITY] = "cool_not_supported"
        elif not isinstance(
            cooling.attributes.get("current_temperature"), (int, float)
        ) or not isfinite(float(cooling.attributes["current_temperature"])):
            errors[CONF_COOLING_ENTITY] = "temperature_not_available"

        heating_entity = data.get(CONF_HEATING_ENTITY)
        if heating_entity:
            heating = self.hass.states.get(heating_entity)
            if heating is None:
                errors[CONF_HEATING_ENTITY] = "entity_not_found"
            elif HVACMode.HEAT not in heating.attributes.get(ATTR_HVAC_MODES, []):
                errors[CONF_HEATING_ENTITY] = "heat_not_supported"
            elif not isinstance(
                heating.attributes.get("current_temperature"),
                (int, float),
            ) or not isfinite(float(heating.attributes["current_temperature"])):
                errors[CONF_HEATING_ENTITY] = "temperature_not_available"
            elif heating_entity == data[CONF_COOLING_ENTITY]:
                errors[CONF_HEATING_ENTITY] = "sources_must_differ"
            elif cooling is not None and not any(
                key in errors for key in (CONF_COOLING_ENTITY, CONF_HEATING_ENTITY)
            ):
                cooling_min = cooling.attributes.get(ATTR_MIN_TEMP)
                cooling_max = cooling.attributes.get(ATTR_MAX_TEMP)
                heating_min = heating.attributes.get(ATTR_MIN_TEMP)
                heating_max = heating.attributes.get(ATTR_MAX_TEMP)
                ranges = (
                    cooling_min,
                    cooling_max,
                    heating_min,
                    heating_max,
                )
                numeric_ranges = tuple(
                    float(value) for value in ranges if isinstance(value, (int, float))
                )
                if len(numeric_ranges) == len(ranges):
                    (
                        cooling_minimum,
                        cooling_maximum,
                        heating_minimum,
                        heating_maximum,
                    ) = numeric_ranges
                    if max(cooling_minimum, heating_minimum) > min(
                        cooling_maximum, heating_maximum
                    ):
                        errors[CONF_HEATING_ENTITY] = (
                            "temperature_ranges_do_not_overlap"
                        )

        sensor = self.hass.states.get(data[CONF_TEMPERATURE_SENSOR])
        try:
            sensor_temperature = float(sensor.state if sensor is not None else "")
        except (TypeError, ValueError):
            errors[CONF_TEMPERATURE_SENSOR] = "temperature_not_available"
        else:
            if not isfinite(sensor_temperature):
                errors[CONF_TEMPERATURE_SENSOR] = "temperature_not_available"

        fan_entity = data.get(CONF_CEILING_FAN)
        if fan_entity:
            fan = self.hass.states.get(fan_entity)
            required_features = (
                FanEntityFeature.DIRECTION
                | FanEntityFeature.TURN_OFF
                | FanEntityFeature.TURN_ON
            )
            if fan is None:
                errors[CONF_CEILING_FAN] = "entity_not_found"
            elif (
                FanEntityFeature(fan.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
                & required_features
                != required_features
            ):
                errors[CONF_CEILING_FAN] = "fan_features_not_supported"
        return errors

    @staticmethod
    def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
        cooling_key = (
            vol.Required(
                CONF_COOLING_ENTITY,
                default=defaults[CONF_COOLING_ENTITY],
            )
            if CONF_COOLING_ENTITY in defaults
            else vol.Required(CONF_COOLING_ENTITY)
        )
        heating_key = (
            vol.Optional(
                CONF_HEATING_ENTITY,
                default=defaults[CONF_HEATING_ENTITY],
            )
            if defaults.get(CONF_HEATING_ENTITY)
            else vol.Optional(CONF_HEATING_ENTITY)
        )
        sensor_key = (
            vol.Required(
                CONF_TEMPERATURE_SENSOR,
                default=defaults[CONF_TEMPERATURE_SENSOR],
            )
            if CONF_TEMPERATURE_SENSOR in defaults
            else vol.Required(CONF_TEMPERATURE_SENSOR)
        )
        fan_key = (
            vol.Optional(
                CONF_CEILING_FAN,
                default=defaults[CONF_CEILING_FAN],
            )
            if defaults.get(CONF_CEILING_FAN)
            else vol.Optional(CONF_CEILING_FAN)
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=defaults.get(CONF_NAME, "Better Climate"),
                ): str,
                cooling_key: EntitySelector(
                    EntitySelectorConfig(domain=CLIMATE_DOMAIN)
                ),
                heating_key: EntitySelector(
                    EntitySelectorConfig(domain=CLIMATE_DOMAIN)
                ),
                sensor_key: EntitySelector(
                    EntitySelectorConfig(
                        domain=SENSOR_DOMAIN,
                        device_class="temperature",
                    )
                ),
                fan_key: EntitySelector(EntitySelectorConfig(domain=FAN_DOMAIN)),
                vol.Optional(
                    CONF_HYSTERESIS,
                    default=defaults.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1,
                        max=2.0,
                        step=0.1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_FORCE_OFFSET,
                    default=defaults.get(CONF_FORCE_OFFSET, DEFAULT_FORCE_OFFSET),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.5,
                        max=3.0,
                        step=0.5,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_MIN_COMMAND_INTERVAL,
                    default=defaults.get(
                        CONF_MIN_COMMAND_INTERVAL,
                        DEFAULT_MIN_COMMAND_INTERVAL,
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=900,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

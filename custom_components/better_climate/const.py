"""Constants for Better Climate."""

DOMAIN = "better_climate"

CONF_COOLING_ENTITY = "cooling_entity"
CONF_HEATING_ENTITY = "heating_entity"
CONF_FAN = "fan"
CONF_LEGACY_CEILING_FAN = "ceiling_fan"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HYSTERESIS = "hysteresis"
CONF_FORCE_OFFSET = "force_offset"
CONF_MIN_COMMAND_INTERVAL = "min_command_interval"
CONF_POWER_OFF_WHEN_COOLING_IDLE = "power_off_when_cooling_idle"

DEFAULT_HYSTERESIS = 0.5
DEFAULT_FORCE_OFFSET = 0.5
DEFAULT_MIN_COMMAND_INTERVAL = 10
DEFAULT_POWER_OFF_WHEN_COOLING_IDLE = False

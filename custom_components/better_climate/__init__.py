"""Better Climate integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_FAN, CONF_LEGACY_CEILING_FAN

PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Better Climate from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Better Climate config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate the ceiling fan option to the generic fan option."""
    if entry.version == 1:
        data = dict(entry.data)
        if CONF_LEGACY_CEILING_FAN in data and CONF_FAN not in data:
            data[CONF_FAN] = data.pop(CONF_LEGACY_CEILING_FAN)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True

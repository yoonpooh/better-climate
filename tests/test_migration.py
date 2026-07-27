"""Migration tests for Better Climate."""

import unittest
from types import SimpleNamespace

from custom_components.better_climate import async_migrate_entry
from custom_components.better_climate.const import (
    CONF_FAN,
    CONF_LEGACY_CEILING_FAN,
)


class MigrationTest(unittest.IsolatedAsyncioTestCase):
    """Verify legacy ceiling fan configuration remains compatible."""

    async def test_migrates_ceiling_fan_to_fan(self) -> None:
        updates = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda entry, **changes: updates.append(changes)
            )
        )
        entry = SimpleNamespace(
            version=1,
            data={CONF_LEGACY_CEILING_FAN: "fan.living_room"},
        )

        self.assertTrue(await async_migrate_entry(hass, entry))
        self.assertEqual(
            updates,
            [{"data": {CONF_FAN: "fan.living_room"}, "version": 2}],
        )

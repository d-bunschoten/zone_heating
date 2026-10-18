"""Select entities for Zone Heating.

Only the global master mode (spec §1.2): ``select.heating_mode`` with options
auto/fallback/off, default ``auto``. RestoreEntity so the chosen mode survives
restarts. No control logic in F1.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ZoneHeatingConfigEntry
from .const import DEFAULT_HEATING_MODE, HEATING_MODES, KEY_HEATING_MODE
from .helpers import hub_device_info


class HeatingModeSelect(SelectEntity, RestoreEntity):
    """The global master-mode selector (auto/fallback/off)."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_name = "Heating mode"
    _attr_options = HEATING_MODES
    _attr_translation_key = "heating_mode"

    def __init__(self, entry: ZoneHeatingConfigEntry) -> None:
        self._attr_unique_id = KEY_HEATING_MODE
        self._attr_current_option = DEFAULT_HEATING_MODE
        self._attr_device_info = hub_device_info(entry)
        # Force the spec entity_id (select.heating_mode).
        self.entity_id = f"select.{KEY_HEATING_MODE}"

    async def async_added_to_hass(self) -> None:
        """Restore the last selected mode."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in HEATING_MODES:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        """Change the master mode."""
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the global select entity."""
    async_add_entities([HeatingModeSelect(entry)])

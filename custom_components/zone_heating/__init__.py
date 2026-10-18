"""The Zone Heating integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER, PLATFORMS


class ZoneHeatingRuntimeData:
    """Container for runtime data kept on the config entry.

    Intentionally minimal in F1 (config surface + entities only). The engine,
    coordinator and backends are attached here in later phases (F2+).
    """


# Typed config entry for runtime data (HA runtime_data style).
ZoneHeatingConfigEntry = ConfigEntry[ZoneHeatingRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> bool:
    """Set up Zone Heating from a config entry."""
    LOGGER.debug("Setting up %s entry %s", DOMAIN, entry.entry_id)

    entry.runtime_data = ZoneHeatingRuntimeData()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when the hub options or the set of zone subentries change, so newly
    # added zones get their device and entities created.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> bool:
    """Unload a Zone Heating config entry."""
    LOGGER.debug("Unloading %s entry %s", DOMAIN, entry.entry_id)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> None:
    """Reload the entry when its options or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)

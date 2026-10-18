"""The Zone Heating integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER

# No platforms are forwarded yet (scaffold phase F0).
PLATFORMS: list[Platform] = []


class ZoneHeatingRuntimeData:
    """Container for runtime data kept on the config entry.

    Intentionally empty in F0; business logic (engine, coordinators) will be
    attached here in later phases.
    """


# Typed config entry for runtime data (HA runtime_data style).
ZoneHeatingConfigEntry = ConfigEntry[ZoneHeatingRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> bool:
    """Set up Zone Heating from a config entry."""
    LOGGER.debug("Setting up %s entry %s", DOMAIN, entry.entry_id)

    # Store runtime data on the entry (no platforms forwarded yet).
    entry.runtime_data = ZoneHeatingRuntimeData()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> bool:
    """Unload a Zone Heating config entry."""
    LOGGER.debug("Unloading %s entry %s", DOMAIN, entry.entry_id)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

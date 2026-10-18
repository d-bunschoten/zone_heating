"""The Zone Heating integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .backend import HeatingBackend
from .backend_tado import TadoBackend
from .const import DOMAIN, LOGGER, PLATFORMS
from .coordinator import ZoneHeatingCoordinator


@dataclass
class ZoneHeatingRuntimeData:
    """Container for runtime data kept on the config entry.

    The coordinator (F2) drives the auto state machine for every zone; the
    backend writes setpoints to the underlying climate entities.
    """

    coordinator: ZoneHeatingCoordinator
    backend: HeatingBackend


# Typed config entry for runtime data (HA runtime_data style).
ZoneHeatingConfigEntry = ConfigEntry[ZoneHeatingRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: ZoneHeatingConfigEntry
) -> bool:
    """Set up Zone Heating from a config entry."""
    LOGGER.debug("Setting up %s entry %s", DOMAIN, entry.entry_id)

    backend = TadoBackend(hass)
    coordinator = ZoneHeatingCoordinator(hass, entry, backend)
    # Attached before forwarding platforms so entities can reach the coordinator
    # from their ``async_added_to_hass`` (e.g. the climate entity subscribes).
    entry.runtime_data = ZoneHeatingRuntimeData(
        coordinator=coordinator, backend=backend
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Now that every source entity exists, wire listeners + tick and evaluate.
    await coordinator.async_setup()

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

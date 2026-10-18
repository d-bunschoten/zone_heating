"""Shared HA-side helpers for Zone Heating.

This module is part of the HA glue layer (it imports Home Assistant). It is
deliberately *not* in ``engine/`` which must stay HA-agnostic.
"""

from __future__ import annotations

from collections.abc import Iterator

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_NAME, DOMAIN, SUBENTRY_TYPE_ZONE


def iter_zone_subentries(
    entry: ConfigEntry,
) -> Iterator[ConfigSubentry]:
    """Yield the zone subentries of the hub config entry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_ZONE:
            yield subentry


def zone_name(subentry: ConfigSubentry) -> str:
    """Return the display name of a zone subentry."""
    return subentry.data.get(CONF_NAME) or subentry.title


def zone_device_info(subentry: ConfigSubentry) -> DeviceInfo:
    """Build the DeviceInfo for a zone's HA device (one device per zone)."""
    return DeviceInfo(
        identifiers={(DOMAIN, subentry.subentry_id)},
        name=zone_name(subentry),
        manufacturer="Zone Heating",
        model="Heating zone",
    )


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build the DeviceInfo for the global hub device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Zone Heating",
        manufacturer="Zone Heating",
        model="Hub",
    )

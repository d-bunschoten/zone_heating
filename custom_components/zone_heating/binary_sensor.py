"""Binary sensor entities for Zone Heating (global + per-zone).

Global (spec §1.2): ``heating_home_present`` (debounced house presence) and
``heating_in_fallback`` (>=1 zone on the Tado schedule). Per-zone (spec §1.1):
``<zone>_present`` (debounced presence) and ``<zone>_window_open`` (mirrors the
configured window sensor). In F1 these exist and default to off; the debounce
and mirror logic land in F2/F5.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZoneHeatingConfigEntry
from .const import (
    KEY_HOME_PRESENT,
    KEY_IN_FALLBACK,
    KEY_PRESENT,
    KEY_WINDOW_OPEN,
)
from .helpers import hub_device_info, iter_zone_subentries, zone_device_info


class _BaseBinarySensor(BinarySensorEntity):
    """A read-only binary sensor that defaults to off (no logic in F1)."""

    _attr_should_poll = False
    _attr_is_on = False


class ZonePresentBinarySensor(_BaseBinarySensor):
    """Debounced per-zone presence."""

    _attr_has_entity_name = True
    _attr_name = "Present"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def __init__(self, subentry: ConfigSubentry) -> None:
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_PRESENT}"
        self._attr_device_info = zone_device_info(subentry)


class ZoneWindowOpenBinarySensor(_BaseBinarySensor):
    """Mirrors the configured window sensor for the zone."""

    _attr_has_entity_name = True
    _attr_name = "Window open"
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(self, subentry: ConfigSubentry) -> None:
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_WINDOW_OPEN}"
        self._attr_device_info = zone_device_info(subentry)


class HomePresentBinarySensor(_BaseBinarySensor):
    """Debounced global house presence."""

    _attr_has_entity_name = False
    _attr_name = "Heating home present"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def __init__(self, device: DeviceInfo) -> None:
        self._attr_unique_id = KEY_HOME_PRESENT
        self._attr_device_info = device
        self.entity_id = f"binary_sensor.{KEY_HOME_PRESENT}"


class InFallbackBinarySensor(_BaseBinarySensor):
    """True when >=1 zone actually runs on the Tado schedule."""

    _attr_has_entity_name = False
    _attr_name = "Heating in fallback"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, device: DeviceInfo) -> None:
        self._attr_unique_id = KEY_IN_FALLBACK
        self._attr_device_info = device
        self.entity_id = f"binary_sensor.{KEY_IN_FALLBACK}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the global and per-zone binary sensor entities."""
    hub = hub_device_info(entry)
    async_add_entities(
        [HomePresentBinarySensor(hub), InFallbackBinarySensor(hub)]
    )

    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [
                ZonePresentBinarySensor(subentry),
                ZoneWindowOpenBinarySensor(subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

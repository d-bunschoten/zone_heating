"""Sensor entities for Zone Heating.

Per-zone ``sensor.<zone>_control_source`` (spec §1.1, §5): reports whether the
zone currently runs on an HA overlay or follows the Tado schedule. In F1 this is
a read-only enum sensor with no value yet (the live mirror is wired up in F5).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZoneHeatingConfigEntry
from .const import CONTROL_SOURCE_OPTIONS, KEY_CONTROL_SOURCE
from .helpers import iter_zone_subentries, zone_device_info


class ControlSourceSensor(SensorEntity):
    """Reports the zone's control source (ha_overlay / tado_schedule)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Control source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROL_SOURCE_OPTIONS
    _attr_translation_key = "control_source"

    def __init__(self, subentry: ConfigSubentry) -> None:
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_CONTROL_SOURCE}"
        self._attr_device_info = zone_device_info(subentry)
        # No value until the backend is wired up (F5).
        self._attr_native_value = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the per-zone sensor entities."""
    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [ControlSourceSensor(subentry)],
            config_subentry_id=subentry.subentry_id,
        )

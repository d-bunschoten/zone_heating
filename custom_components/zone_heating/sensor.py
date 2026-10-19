"""Sensor entities for Zone Heating.

Per-zone ``sensor.<zone>_control_source`` (spec §1.1, §5): a **live mirror** of the
zone's ``switch.<zone>_schedule`` — ``ha_overlay`` while HA holds an overlay (switch
off), ``tado_schedule`` while the zone follows its Tado schedule (switch on), and
unknown when no switch is resolvable. The coordinator computes the value; this
entity reflects it and re-renders on the per-zone dispatcher signal.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZoneHeatingConfigEntry
from .const import (
    CONTROL_SOURCE_OPTIONS,
    KEY_CONTROL_SOURCE,
    KEY_OVERRIDE_ENDS,
    SIGNAL_ZONE_UPDATE,
)
from .coordinator import ZoneHeatingCoordinator
from .helpers import iter_zone_subentries, zone_device_info


class ControlSourceSensor(SensorEntity):
    """Reports the zone's control source (ha_overlay / tado_schedule)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Control source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROL_SOURCE_OPTIONS
    _attr_translation_key = "control_source"

    def __init__(
        self, entry: ZoneHeatingConfigEntry, subentry: ConfigSubentry
    ) -> None:
        self._entry = entry
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_CONTROL_SOURCE}"
        self._attr_device_info = zone_device_info(subentry)
        self._coordinator: ZoneHeatingCoordinator | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the zone's update signal (control_source is live)."""
        await super().async_added_to_hass()
        self._coordinator = self._entry.runtime_data.coordinator
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ZONE_UPDATE.format(subentry_id=self._subentry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        if self._coordinator is None:
            return None
        return self._coordinator.zone_control_source(self._subentry_id)


class OverrideEndsSensor(SensorEntity):
    """End of the respect-window / boost as a live timestamp (spec §1.1, §3).

    A ``timestamp`` device class lets the HA UI render a live "ends in 1 h 45 m"
    countdown without the backend having to tick. ``None`` (the sensor shows
    ``unknown``) whenever no respect-window/boost runs — in ``auto``, a sticky
    ``away`` preset or a sticky ``off`` zone. The coordinator owns the timer; this
    entity reflects its end and re-renders on the per-zone dispatcher signal.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Override ends"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "override_ends"

    def __init__(
        self, entry: ZoneHeatingConfigEntry, subentry: ConfigSubentry
    ) -> None:
        self._entry = entry
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_OVERRIDE_ENDS}"
        self._attr_device_info = zone_device_info(subentry)
        self._coordinator: ZoneHeatingCoordinator | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the zone's update signal (the timer end is live)."""
        await super().async_added_to_hass()
        self._coordinator = self._entry.runtime_data.coordinator
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ZONE_UPDATE.format(subentry_id=self._subentry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        if self._coordinator is None:
            return None
        return self._coordinator.zone_override_end(self._subentry_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the per-zone sensor entities."""
    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [
                ControlSourceSensor(entry, subentry),
                OverrideEndsSensor(entry, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

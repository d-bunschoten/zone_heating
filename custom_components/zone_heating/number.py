"""Number entities for Zone Heating (global + per-zone setpoints).

Per-zone setpoints (``away``/``standby``/``active``/``boost``) are required
RestoreNumber entities so user changes persist across restarts (spec §1.1).
Global numbers (boost duration, override timeout) are the dashboard knobs from
spec §1.2. No control logic here (F1): values are stored defaults only.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZoneHeatingConfigEntry
from .const import (
    DEFAULT_ACTIVE_TEMP,
    DEFAULT_AWAY_TEMP,
    DEFAULT_BOOST_DURATION,
    DEFAULT_BOOST_TEMP,
    DEFAULT_OVERRIDE_TIMEOUT,
    DEFAULT_STANDBY_TEMP,
    KEY_ACTIVE_TEMP,
    KEY_AWAY_TEMP,
    KEY_BOOST_DURATION,
    KEY_BOOST_TEMP,
    KEY_OVERRIDE_TIMEOUT,
    KEY_STANDBY_TEMP,
    MAX_TEMP,
    MIN_TEMP,
    TEMP_STEP,
)
from .helpers import hub_device_info, iter_zone_subentries, zone_device_info


class _BaseNumber(RestoreNumber):
    """A persistent number whose value survives restarts."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, unique_id: str, default: float) -> None:
        self._attr_unique_id = unique_id
        self._default = default
        self._attr_native_value = default

    async def async_added_to_hass(self) -> None:
        """Restore the last stored value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Store a new value."""
        self._attr_native_value = value
        self.async_write_ha_state()


class _TempNumber(_BaseNumber):
    """A temperature setpoint number."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = MIN_TEMP
    _attr_native_max_value = MAX_TEMP
    _attr_native_step = TEMP_STEP
    _attr_mode = NumberMode.BOX


class _DurationNumber(_BaseNumber):
    """A duration (minutes) number."""

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 1440
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX


class ZoneTempNumber(_TempNumber):
    """A per-zone setpoint (away/standby/active/boost)."""

    def __init__(
        self,
        subentry: ConfigSubentry,
        key: str,
        name: str,
        default: float,
    ) -> None:
        super().__init__(f"{subentry.subentry_id}_{key}", default)
        self._attr_name = name
        self._attr_device_info = zone_device_info(subentry)


class GlobalDurationNumber(_DurationNumber):
    """A global duration number (boost duration / override timeout)."""

    _attr_has_entity_name = False

    def __init__(
        self, device: DeviceInfo, key: str, name: str, default: float
    ) -> None:
        super().__init__(key, default)
        self._attr_name = name
        self._attr_device_info = device
        self.entity_id = f"number.{key}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the global and per-zone number entities."""
    hub = hub_device_info(entry)
    async_add_entities(
        [
            GlobalDurationNumber(
                hub,
                KEY_BOOST_DURATION,
                "Heating boost duration",
                DEFAULT_BOOST_DURATION,
            ),
            GlobalDurationNumber(
                hub,
                KEY_OVERRIDE_TIMEOUT,
                "Heating override timeout",
                DEFAULT_OVERRIDE_TIMEOUT,
            ),
        ]
    )

    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [
                ZoneTempNumber(
                    subentry, KEY_AWAY_TEMP, "Away temp", DEFAULT_AWAY_TEMP
                ),
                ZoneTempNumber(
                    subentry,
                    KEY_STANDBY_TEMP,
                    "Standby temp",
                    DEFAULT_STANDBY_TEMP,
                ),
                ZoneTempNumber(
                    subentry,
                    KEY_ACTIVE_TEMP,
                    "Active temp",
                    DEFAULT_ACTIVE_TEMP,
                ),
                ZoneTempNumber(
                    subentry,
                    KEY_BOOST_TEMP,
                    "Boost temp",
                    DEFAULT_BOOST_TEMP,
                ),
            ],
            config_subentry_id=subentry.subentry_id,
        )

"""Climate entity for Zone Heating (one per zone, the device's main entity).

Spec §1.1, §4. The entity wraps a zone's target climate entities and is a thin
view + action forwarder over the coordinator, which owns the authoritative
per-zone state (auto decision in F2; manual ``hvac_mode``/preset/Custom + timers
in F3). Display (``hvac_mode``, ``preset_mode``, target setpoint, attributes)
reads back from the coordinator; the user actions delegate to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import ZoneHeatingConfigEntry
from .const import (
    ATTR_CONTROL_SOURCE,
    ATTR_OVERRIDE_ACTIVE,
    ATTR_OVERRIDE_REMAINING,
    ATTR_PRESENT,
    ATTR_TARGET_ENTITIES,
    ATTR_TARGET_SETPOINT,
    ATTR_WINDOW_OPEN,
    ATTR_ZONE_STATE,
    CONF_TARGETS,
    DEFAULT_ACTIVE_TEMP,
    KEY_CLIMATE,
    MAX_TEMP,
    MIN_TEMP,
    SIGNAL_ZONE_UPDATE,
    TEMP_STEP,
    ZONE_PRESET_MODES,
)
from .coordinator import ZoneHeatingCoordinator, ZoneRuntimeState
from .engine.state_machine import STAND_OFF, STAND_WINDOW_OPEN
from .helpers import iter_zone_subentries, zone_device_info, zone_name


@dataclass
class _ZoneClimateRestoreData(ExtraStoredData):
    """Persisted manual state so a zone survives an HA restart (spec §9).

    The authoritative manual state (hvac_mode/preset/Custom + respect-window) lives
    in the coordinator, which is recreated cold on restart; the climate entity is
    the RestoreEntity that carries it across so the coordinator's first evaluation
    resumes the user's stand instead of defaulting to ``auto``.
    """

    hvac_mode: str
    preset: str | None
    custom_setpoint: float | None
    override_remaining_min: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hvac_mode": self.hvac_mode,
            "preset": self.preset,
            "custom_setpoint": self.custom_setpoint,
            "override_remaining_min": self.override_remaining_min,
        }


class ZoneClimate(ClimateEntity, RestoreEntity):
    """The user-facing thermostat for a heating zone."""

    _attr_has_entity_name = True
    _attr_name = None  # main entity of the device -> entity_id = climate.<zone>
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = ZONE_PRESET_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP
    # HA requires this when using the enum-based preset/hvac turn on/off flags.
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, entry: ZoneHeatingConfigEntry, subentry: ConfigSubentry
    ) -> None:
        self._entry = entry
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_CLIMATE}"
        self._attr_device_info = zone_device_info(subentry)
        self._targets: list[str] = list(subentry.data.get(CONF_TARGETS, []))
        # Local fallback shown before the first evaluation only.
        self._attr_target_temperature = DEFAULT_ACTIVE_TEMP
        self._zone_name = zone_name(subentry)
        self._coordinator: ZoneHeatingCoordinator | None = None

    async def async_added_to_hass(self) -> None:
        """Restore manual state, subscribe to updates, track the targets (spec §9)."""
        await super().async_added_to_hass()
        self._coordinator = self._entry.runtime_data.coordinator
        # Runs before the coordinator's ``async_setup``: hand the restored manual
        # state to it so the first evaluation resumes the user's stand (spec §9).
        restored = await self.async_get_last_extra_data()
        if restored is not None:
            data = restored.as_dict()
            self._coordinator.restore_zone_manual(
                self._subentry_id,
                hvac_mode=data.get("hvac_mode", HVACMode.AUTO.value),
                preset=data.get("preset"),
                custom_setpoint=data.get("custom_setpoint"),
                remaining_min=data.get("override_remaining_min"),
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ZONE_UPDATE.format(subentry_id=self._subentry_id),
                self._handle_coordinator_update,
            )
        )
        if self._targets:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._targets, self._handle_target_change
                )
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_target_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self.async_write_ha_state()

    @property
    def _runtime(self) -> ZoneRuntimeState | None:
        if self._coordinator is None:
            return None
        return self._coordinator.zone_runtime.get(self._subentry_id)

    @property
    def hvac_mode(self) -> HVACMode:
        """The zone's hvac_mode is owned by the coordinator (spec §3, §4)."""
        if self._coordinator is None:
            return HVACMode.AUTO
        return HVACMode(self._coordinator.zone_hvac_mode(self._subentry_id))

    @property
    def current_temperature(self) -> float | None:
        """Average of the targets' reported current temperatures."""
        temps: list[float] = []
        for entity_id in self._targets:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            value = state.attributes.get("current_temperature")
            if value is not None:
                try:
                    temps.append(float(value))
                except (TypeError, ValueError):
                    continue
        if not temps:
            return None
        return round(sum(temps) / len(temps), 1)

    @property
    def target_temperature(self) -> float | None:
        """Follow the coordinator's chosen/forced setpoint (spec §3, §4).

        While the zone is off or window-open the device holds frost protection, so
        the dial shows the off/frost temp (not a stale last setpoint); it returns to
        the chosen/auto setpoint as soon as the zone leaves off (spec §4/§8).
        """
        runtime = self._runtime
        if runtime is not None and runtime.setpoint is not None:
            return runtime.setpoint
        coord = self._coordinator
        if (
            coord is not None
            and runtime is not None
            and runtime.zone_state in (STAND_OFF, STAND_WINDOW_OPEN)
        ):
            off_temp = coord.zone_off_temp(self._subentry_id)
            if off_temp is not None:
                return off_temp
        return self._attr_target_temperature

    @property
    def preset_mode(self) -> str | None:
        """Reflect the current stand; Custom shows ``none`` (spec §4).

        In ``auto`` this is the auto-chosen stand; in ``heat`` the forced preset
        (or ``None`` for Custom); in ``off`` it is ``None``. The coordinator's
        runtime already encodes all three cases in ``preset_mode``.
        """
        runtime = self._runtime
        return runtime.preset_mode if runtime else None

    @property
    def extra_restore_state_data(self) -> _ZoneClimateRestoreData | None:
        """Persist the manual state so it survives a restart (spec §9)."""
        coord = self._coordinator
        if coord is None:
            return None
        sid = self._subentry_id
        return _ZoneClimateRestoreData(
            hvac_mode=coord.zone_stored_hvac_mode(sid),
            preset=coord.zone_manual_preset(sid),
            custom_setpoint=coord.zone_custom_setpoint(sid),
            override_remaining_min=coord.zone_override_remaining_min(sid),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the zone status (spec §4; control_source live mirror, spec §5)."""
        runtime = self._runtime
        coord = self._coordinator
        sid = self._subentry_id
        return {
            ATTR_ZONE_STATE: runtime.zone_state if runtime else None,
            ATTR_CONTROL_SOURCE: (
                coord.zone_control_source(sid) if coord else None
            ),
            ATTR_OVERRIDE_ACTIVE: (
                coord.zone_override_active(sid) if coord else False
            ),
            ATTR_OVERRIDE_REMAINING: (
                coord.zone_override_remaining_min(sid) if coord else None
            ),
            ATTR_PRESENT: runtime.present if runtime else False,
            ATTR_WINDOW_OPEN: runtime.window_open if runtime else False,
            ATTR_TARGET_SETPOINT: runtime.setpoint if runtime else None,
            ATTR_TARGET_ENTITIES: self._targets,
        }

    # --- Interaction (spec §4) ----------------------------------------------
    async def async_set_temperature(self, **kwargs: object) -> None:
        """Dial a temperature -> Custom (hvac_mode=heat, preset=none).

        A zone that is manually ``off`` turns back on to Custom when dialed; but a
        dial during **window-open** is ignored by the coordinator (the safety
        modifier wins, spec §3/§4).
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._attr_target_temperature = float(temperature)  # type: ignore[arg-type]
        if self._coordinator is not None:
            await self._coordinator.async_set_zone_custom(
                self._subentry_id, float(temperature)  # type: ignore[arg-type]
            )
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch hvac_mode (auto/heat/off)."""
        if self._coordinator is not None:
            await self._coordinator.async_set_zone_hvac_mode(
                self._subentry_id, hvac_mode.value
            )
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Force a stand -> hvac_mode=heat with that preset (spec §4)."""
        if self._coordinator is not None:
            await self._coordinator.async_set_zone_preset(
                self._subentry_id, preset_mode
            )
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the zone on (back to auto)."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        """Turn the zone off."""
        await self.async_set_hvac_mode(HVACMode.OFF)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one climate entity per zone."""
    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [ZoneClimate(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )

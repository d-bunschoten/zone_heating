"""Climate entity for Zone Heating (one per zone, the device's main entity).

Spec §1.1, §4. The entity wraps a zone's target climate entities. In ``auto``
(F2) it reflects the coordinator's decision: ``zone_state``, target setpoint and
``preset_mode`` follow the state machine. Manual ``heat``/``off`` interaction
(override, boost, Custom) is F3 — those actions still store locally here and the
coordinator skips non-``auto`` zones.
"""

from __future__ import annotations

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

from . import ZoneHeatingConfigEntry
from .const import (
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
from .helpers import iter_zone_subentries, zone_device_info, zone_name


class ZoneClimate(ClimateEntity):
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
        self._attr_hvac_mode = HVACMode.AUTO
        # Local fallbacks used only outside auto (F3) / before the first eval.
        self._attr_preset_mode = None
        self._attr_target_temperature = DEFAULT_ACTIVE_TEMP
        self._zone_name = zone_name(subentry)
        self._coordinator: ZoneHeatingCoordinator | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates and track the target entities."""
        await super().async_added_to_hass()
        self._coordinator = self._entry.runtime_data.coordinator
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
        """In auto, follow the coordinator's chosen setpoint."""
        runtime = self._runtime
        if (
            self._attr_hvac_mode == HVACMode.AUTO
            and runtime is not None
            and runtime.setpoint is not None
        ):
            return runtime.setpoint
        return self._attr_target_temperature

    @property
    def preset_mode(self) -> str | None:
        """In auto, reflect the coordinator's chosen stand (spec §4)."""
        runtime = self._runtime
        if self._attr_hvac_mode == HVACMode.AUTO and runtime is not None:
            return runtime.preset_mode
        return self._attr_preset_mode

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the zone status (spec §4; control_source/override in F3/F5)."""
        runtime = self._runtime
        return {
            ATTR_ZONE_STATE: runtime.zone_state if runtime else None,
            ATTR_TARGET_SETPOINT: runtime.setpoint if runtime else None,
            ATTR_PRESENT: runtime.present if runtime else False,
            ATTR_WINDOW_OPEN: runtime.window_open if runtime else False,
            ATTR_TARGET_ENTITIES: self._targets,
        }

    # --- Interaction --------------------------------------------------------
    # heat/preset/Custom/boost semantics are F3; F2 stores locally and asks the
    # coordinator to re-evaluate (so returning to auto takes effect at once).
    async def async_set_temperature(self, **kwargs: object) -> None:
        """Store a new target temperature (full Custom handling is F3)."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temperature)  # type: ignore[arg-type]
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Store a new hvac mode and re-evaluate."""
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        if self._coordinator is not None:
            self._coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Store a new preset mode (manual override wiring is F3)."""
        self._attr_preset_mode = preset_mode
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

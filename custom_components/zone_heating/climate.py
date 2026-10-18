"""Climate entity for Zone Heating (one per zone, the device's main entity).

Spec §1.1, §4. The entity wraps a zone's target climate entities. In F1 it
exposes the correct shape (hvac_modes, preset_modes, supported_features) and
reflects the targets' current temperature, but drives nothing: the state machine
and backend writes land in F2+.
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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import ZoneHeatingConfigEntry
from .const import (
    CONF_TARGETS,
    DEFAULT_ACTIVE_TEMP,
    KEY_CLIMATE,
    MAX_TEMP,
    MIN_TEMP,
    TEMP_STEP,
    ZONE_PRESET_MODES,
)
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

    def __init__(self, subentry: ConfigSubentry) -> None:
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_CLIMATE}"
        self._attr_device_info = zone_device_info(subentry)
        self._targets: list[str] = list(subentry.data.get(CONF_TARGETS, []))
        # Defaults reflected until the engine drives the zone (F2+).
        self._attr_hvac_mode = HVACMode.AUTO
        self._attr_preset_mode = None
        self._attr_target_temperature = DEFAULT_ACTIVE_TEMP
        self._zone_name = zone_name(subentry)

    async def async_added_to_hass(self) -> None:
        """Track the target entities so current_temperature stays fresh."""
        await super().async_added_to_hass()
        if self._targets:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._targets, self._handle_target_change
                )
            )

    @callback
    def _handle_target_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self.async_write_ha_state()

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
    def extra_state_attributes(self) -> dict[str, list[str]]:
        """Expose the wrapped targets (full status attributes come in F2+)."""
        return {"target_entities": self._targets}

    # --- Interaction (stores locally only in F1; no backend writes) ---------
    async def async_set_temperature(self, **kwargs: object) -> None:
        """Store a new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temperature)  # type: ignore[arg-type]
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Store a new hvac mode."""
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Store a new preset mode."""
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
            [ZoneClimate(subentry)],
            config_subentry_id=subentry.subentry_id,
        )

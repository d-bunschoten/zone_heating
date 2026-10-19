"""Binary sensor entities for Zone Heating (global + per-zone).

Global (spec §1.2): ``heating_home_present`` (debounced house presence) and
``heating_in_fallback`` (>=1 zone on the Tado schedule). Per-zone (spec §1.1):
``<zone>_present`` (debounced presence) and ``<zone>_window_open`` (debounced
mirror of the configured window sensor, spec §3).

Presence (spec §6) and the window sensor (spec §3) share one debounced-mirror
base: a source entity/template with an on-delay and off-delay. ``in_fallback``
reflects the coordinator's live fallback flag (spec §9).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    TrackTemplate,
    async_call_later,
    async_track_state_change_event,
    async_track_template_result,
)
from homeassistant.helpers.template import Template, result_as_boolean

from . import ZoneHeatingConfigEntry
from .const import (
    CONF_HOME_OFF_DELAY,
    CONF_HOME_ON_DELAY,
    CONF_HOME_PRESENCE_ENTITY,
    CONF_HOME_PRESENCE_TEMPLATE,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_PRESENCE_ENTITY,
    CONF_PRESENCE_TEMPLATE,
    CONF_WINDOW_CLOSE_DELAY,
    CONF_WINDOW_OPEN_DELAY,
    CONF_WINDOW_SENSOR,
    DEFAULT_HOME_OFF_DELAY,
    DEFAULT_HOME_ON_DELAY,
    DEFAULT_OFF_DELAY,
    DEFAULT_ON_DELAY,
    DEFAULT_WINDOW_CLOSE_DELAY,
    DEFAULT_WINDOW_OPEN_DELAY,
    KEY_HOME_PRESENT,
    KEY_IN_FALLBACK,
    KEY_PRESENT,
    KEY_WINDOW_OPEN,
    PRESENCE_TRUTHY_STATES,
    SIGNAL_GLOBAL_UPDATE,
)
from .coordinator import ZoneHeatingCoordinator
from .helpers import hub_device_info, iter_zone_subentries, zone_device_info


class _DebouncedBinary(BinarySensorEntity):
    """A binary sensor mirroring a source (entity/template) with on/off delays.

    Source is either an entity (truthy = on/home/open/...) or a small template.
    Without a source the sensor stays at ``default_on``. The first reading applies
    immediately; later flips wait out the relevant delay and are cancelled if the
    source reverts within it (spec §3 window close-delay, spec §6 presence demping).
    """

    _attr_should_poll = False
    _truthy_states = PRESENCE_TRUTHY_STATES

    def __init__(
        self,
        *,
        source_entity: str | None,
        source_template: str | None,
        on_delay_min: float,
        off_delay_min: float,
        default_on: bool,
    ) -> None:
        self._source_entity = source_entity
        self._source_template_str = source_template
        self._template: Template | None = None
        self._on_delay = timedelta(minutes=on_delay_min)
        self._off_delay = timedelta(minutes=off_delay_min)
        self._attr_is_on = default_on
        self._pending: bool | None = None
        self._pending_cancel = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._source_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._source_entity], self._handle_entity
                )
            )
            self._apply_raw(self._read_entity_raw(), immediate=True)
        elif self._source_template_str:
            self._template = Template(self._source_template_str, self.hass)
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(self._template, None)],
                self._handle_template,
            )
            self.async_on_remove(info.async_remove)
            self._apply_raw(self._render_template_raw(), immediate=True)
        # else: no source -> stay at default_on (never debounced).

    # --- raw source reads ---------------------------------------------------
    def _read_entity_raw(self) -> bool:
        state = self.hass.states.get(self._source_entity)
        if state is None:
            return False
        return state.state.lower() in self._truthy_states

    def _render_template_raw(self) -> bool:
        try:
            return result_as_boolean(self._template.async_render())
        except Exception:  # noqa: BLE001 - a bad template reads as "not on"
            return False

    @callback
    def _handle_entity(self, event: Event[EventStateChangedData]) -> None:
        self._apply_raw(self._read_entity_raw())

    @callback
    def _handle_template(self, event, updates) -> None:
        for update in updates:
            result = update.result
            if isinstance(result, Exception):
                self._apply_raw(False)
            else:
                self._apply_raw(result_as_boolean(result))

    # --- debounce -----------------------------------------------------------
    @callback
    def _apply_raw(self, raw: bool, *, immediate: bool = False) -> None:
        if raw == self._attr_is_on:
            self._cancel_pending()
            return
        if self._pending is raw:
            return  # transition to this target already scheduled
        self._cancel_pending()
        delay = self._on_delay if raw else self._off_delay
        if immediate or delay.total_seconds() <= 0:
            self._set_state(raw)
            return
        self._pending = raw
        self._pending_cancel = async_call_later(self.hass, delay, self._fire)

    @callback
    def _fire(self, _now) -> None:
        target = self._pending
        self._pending = None
        self._pending_cancel = None
        if target is not None:
            self._set_state(target)

    @callback
    def _cancel_pending(self) -> None:
        if self._pending_cancel is not None:
            self._pending_cancel()
        self._pending_cancel = None
        self._pending = None

    @callback
    def _set_state(self, value: bool) -> None:
        self._attr_is_on = value
        self.async_write_ha_state()


class _DebouncedPresence(_DebouncedBinary):
    """A debounced presence mirror (spec §6)."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE


class ZonePresentBinarySensor(_DebouncedPresence):
    """Debounced per-zone presence (spec §6)."""

    _attr_has_entity_name = True
    _attr_name = "Present"

    def __init__(self, subentry: ConfigSubentry) -> None:
        super().__init__(
            source_entity=subentry.data.get(CONF_PRESENCE_ENTITY),
            source_template=subentry.data.get(CONF_PRESENCE_TEMPLATE),
            on_delay_min=subentry.data.get(CONF_ON_DELAY, DEFAULT_ON_DELAY),
            off_delay_min=subentry.data.get(CONF_OFF_DELAY, DEFAULT_OFF_DELAY),
            # No presence source -> zone is never Active (spec §6).
            default_on=False,
        )
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_PRESENT}"
        self._attr_device_info = zone_device_info(subentry)


class HomePresentBinarySensor(_DebouncedPresence):
    """Debounced global house presence; drives Away (spec §1.2, §6)."""

    _attr_has_entity_name = False
    _attr_name = "Heating home present"

    def __init__(
        self, entry: ZoneHeatingConfigEntry, device: DeviceInfo
    ) -> None:
        super().__init__(
            source_entity=entry.options.get(CONF_HOME_PRESENCE_ENTITY),
            source_template=entry.options.get(CONF_HOME_PRESENCE_TEMPLATE),
            on_delay_min=entry.options.get(
                CONF_HOME_ON_DELAY, DEFAULT_HOME_ON_DELAY
            ),
            off_delay_min=entry.options.get(
                CONF_HOME_OFF_DELAY, DEFAULT_HOME_OFF_DELAY
            ),
            # No home source configured -> reads not-present (keeps the entity's
            # documented default off). The house source is normally configured
            # (spec §1.3); until it is, zones fall to Away rather than heating.
            default_on=False,
        )
        self._attr_unique_id = KEY_HOME_PRESENT
        self._attr_device_info = device
        self.entity_id = f"binary_sensor.{KEY_HOME_PRESENT}"


class ZoneWindowOpenBinarySensor(_DebouncedBinary):
    """Debounced mirror of the configured window sensor (spec §3).

    Open is reacted to on the open-delay (default 0 = at once, for safety); closing
    waits out the close-delay so a briefly cracked window or a flapping reed sensor
    does not whipsaw the zone's setpoint. No window sensor configured -> stays off.
    """

    _attr_has_entity_name = True
    _attr_name = "Window open"
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(self, subentry: ConfigSubentry) -> None:
        super().__init__(
            source_entity=subentry.data.get(CONF_WINDOW_SENSOR),
            source_template=None,
            on_delay_min=subentry.data.get(
                CONF_WINDOW_OPEN_DELAY, DEFAULT_WINDOW_OPEN_DELAY
            ),
            off_delay_min=subentry.data.get(
                CONF_WINDOW_CLOSE_DELAY, DEFAULT_WINDOW_CLOSE_DELAY
            ),
            default_on=False,
        )
        self._attr_unique_id = f"{subentry.subentry_id}_{KEY_WINDOW_OPEN}"
        self._attr_device_info = zone_device_info(subentry)


class InFallbackBinarySensor(BinarySensorEntity):
    """True when >=1 zone runs on the Tado schedule, or master mode is fallback.

    Reflects the coordinator's live fallback flag (spec §1.2, §9): an unexpected
    fallback (a zone back on its Tado schedule while HA expects an overlay) or the
    deliberate ``fallback`` master mode.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Heating in fallback"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, entry: ZoneHeatingConfigEntry, device: DeviceInfo
    ) -> None:
        self._entry = entry
        self._attr_unique_id = KEY_IN_FALLBACK
        self._attr_device_info = device
        self.entity_id = f"binary_sensor.{KEY_IN_FALLBACK}"
        self._coordinator: ZoneHeatingCoordinator | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._coordinator = self._entry.runtime_data.coordinator
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_GLOBAL_UPDATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator and self._coordinator.in_fallback())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZoneHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the global and per-zone binary sensor entities."""
    hub = hub_device_info(entry)
    async_add_entities(
        [HomePresentBinarySensor(entry, hub), InFallbackBinarySensor(entry, hub)]
    )

    for subentry in iter_zone_subentries(entry):
        async_add_entities(
            [
                ZonePresentBinarySensor(subentry),
                ZoneWindowOpenBinarySensor(subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

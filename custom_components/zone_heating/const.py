"""Constants for the Zone Heating integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform

DOMAIN = "zone_heating"

LOGGER = logging.getLogger(__package__)

# Platforms forwarded from the config entry. Each of these creates the global
# (hub) entities and the per-zone (subentry) entities for its domain.
PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

# --- Subentries -------------------------------------------------------------
# Each configured zone is a config *subentry* of type "zone" on the single hub
# config entry. One subentry == one HA device == all per-zone entities.
SUBENTRY_TYPE_ZONE = "zone"

# --- Zone (subentry) config keys (spec §1.0, §1.1, §1.3, §6) ----------------
CONF_NAME = "name"
CONF_TARGETS = "targets"
CONF_PRESENCE_ENTITY = "presence_entity"
CONF_PRESENCE_TEMPLATE = "presence_template"
CONF_ON_DELAY = "on_delay"
CONF_OFF_DELAY = "off_delay"
CONF_WINDOW_SENSOR = "window_sensor"
# Window-open debounce (minutes, spec §3). Open is reacted to immediately (safety);
# closing waits out the close-delay before the zone returns to its chosen mode, so
# a briefly cracked window does not whipsaw the setpoint.
CONF_WINDOW_OPEN_DELAY = "window_open_delay"
CONF_WINDOW_CLOSE_DELAY = "window_close_delay"
# Optional explicit ``switch.<zone>_schedule`` override (spec §5). When unset the
# coordinator derives it from the zone's first target (``switch.<object_id>_schedule``).
CONF_SCHEDULE_SWITCH = "schedule_switch"
CONF_STANDBY_SCHEDULE = "standby_schedule"
CONF_ACTIVE_SCHEDULE = "active_schedule"
# Off/frost-protection baseline temperature written to Tado in master mode Off
# (spec §8: the off-baseline is configured per zone; the normal baseline is
# derived). A single flat temperature is the simplest fit for "vorstbeveiliging".
CONF_OFF_BASELINE_TEMP = "off_baseline_temp"

# --- Global (hub / options-flow) config keys (spec §1.2, §1.3) --------------
CONF_HOME_PRESENCE_ENTITY = "home_presence_entity"
CONF_HOME_PRESENCE_TEMPLATE = "home_presence_template"
CONF_HOME_ON_DELAY = "home_on_delay"
CONF_HOME_OFF_DELAY = "home_off_delay"
CONF_DEAD_MANS_SWITCH = "dead_mans_switch"
CONF_OVERLAY_TERMINATION = "overlay_termination"
CONF_OVERLAY_TIMER_DURATION = "overlay_timer_duration"

OVERLAY_NEXT_BLOCK = "next_block"
OVERLAY_TIMER = "timer"
OVERLAY_TERMINATION_OPTIONS = [OVERLAY_NEXT_BLOCK, OVERLAY_TIMER]

# --- Defaults ---------------------------------------------------------------
# Presence debounce (minutes). Spec §6 proposes office on 0 / off 10,
# living room on 1 / off 10; we take the common 0 / 10 as the generic default.
DEFAULT_ON_DELAY = 0
DEFAULT_OFF_DELAY = 10
DEFAULT_HOME_ON_DELAY = 0
DEFAULT_HOME_OFF_DELAY = 10
# Window-open debounce (minutes, spec §3). Open 0 = react at once (safety: a cold
# draught must cut heating immediately). Close 5 = a short close-delay so a window
# opened briefly for airing, or a flapping reed sensor, does not bounce the zone
# back on only to shut it off again seconds later.
DEFAULT_WINDOW_OPEN_DELAY = 1
DEFAULT_WINDOW_CLOSE_DELAY = 5
DEFAULT_DEAD_MANS_SWITCH = True
DEFAULT_OVERLAY_TERMINATION = OVERLAY_NEXT_BLOCK
DEFAULT_OVERLAY_TIMER_DURATION = 60

# Per-zone setpoint defaults (°C). Spec §3 ordering is away < standby < active
# (away = nobody home, so coolest). The F1 brief's "e.g. 18/15/20" is
# illustrative ("e.g."); we use a monotonic sensible default instead.
DEFAULT_AWAY_TEMP = 16.0
DEFAULT_STANDBY_TEMP = 18.0
DEFAULT_ACTIVE_TEMP = 20.0
# Per-zone boost comfort setpoint (spec §1.1, §3). Warmer than Active so a boost
# is a noticeable lift; the boost *duration* stays global.
DEFAULT_BOOST_TEMP = 23.0
# Frost-protection setpoint for the off-baseline (spec §8); matches Tado's own
# frost protection (5 °C) and the integration's MIN_TEMP.
DEFAULT_OFF_BASELINE_TEMP = 5.0

# Temperature number bounds (shared by zone setpoints, boost temp, climate).
MIN_TEMP = 5.0
MAX_TEMP = 30.0
TEMP_STEP = 0.5

# Global entity defaults (spec §1.2).
DEFAULT_BOOST_DURATION = 60  # minutes
DEFAULT_OVERRIDE_TIMEOUT = 120  # minutes

# --- Master mode (select.heating_mode) (spec §1.2, §2) ----------------------
MODE_AUTO = "auto"
MODE_FALLBACK = "fallback"
MODE_OFF = "off"
HEATING_MODES = [MODE_AUTO, MODE_FALLBACK, MODE_OFF]
DEFAULT_HEATING_MODE = MODE_AUTO

# --- Climate presets (spec §1.1, §4) ----------------------------------------
PRESET_AWAY = "away"
PRESET_STANDBY = "standby"
PRESET_ACTIVE = "active"
PRESET_BOOST = "boost"
ZONE_PRESET_MODES = [PRESET_AWAY, PRESET_STANDBY, PRESET_ACTIVE, PRESET_BOOST]

# --- control_source values (spec §1.1, §5) ----------------------------------
CONTROL_SOURCE_HA_OVERLAY = "ha_overlay"
CONTROL_SOURCE_TADO_SCHEDULE = "tado_schedule"
CONTROL_SOURCE_OPTIONS = [CONTROL_SOURCE_HA_OVERLAY, CONTROL_SOURCE_TADO_SCHEDULE]

# --- Global entity keys (used for unique_id + object_id) ---------------------
# These map 1:1 to the spec §1.2 entity_ids (select.heating_mode, etc.).
KEY_HEATING_MODE = "heating_mode"
KEY_BOOST_DURATION = "heating_boost_duration"
KEY_OVERRIDE_TIMEOUT = "heating_override_timeout"
KEY_HOME_PRESENT = "heating_home_present"
KEY_IN_FALLBACK = "heating_in_fallback"

# --- Per-zone entity key suffixes (used for unique_id) -----------------------
KEY_CLIMATE = "climate"
KEY_AWAY_TEMP = "away_temp"
KEY_STANDBY_TEMP = "standby_temp"
KEY_ACTIVE_TEMP = "active_temp"
# Per-zone boost setpoint (spec §1.1, §3). Replaces the former global
# ``number.heating_boost_temp`` so boost comfort is tunable per room.
KEY_BOOST_TEMP = "boost_temp"
KEY_CONTROL_SOURCE = "control_source"
KEY_PRESENT = "present"
KEY_WINDOW_OPEN = "window_open"
# Per-zone respect-window / boost end as a timestamp sensor (spec §1.1). A
# timestamp lets the HA UI show a live "ends in …" countdown without the backend
# having to tick; ``unknown`` while no respect-window/boost runs.
KEY_OVERRIDE_ENDS = "override_ends"

# --- Coordinator (F2) -------------------------------------------------------
# Periodic re-evaluation so schedule-block boundaries are crossed even without a
# source state change (spec §3: "triggers + periodieke tick"). Kept well inside
# any overlay termination (overlay/heartbeat handling itself is F4).
TICK_INTERVAL_MINUTES = 5

# --- Dead-man's-switch heartbeat (F4, spec §7) ------------------------------
# When the dead-man's-switch is on, the coordinator re-asserts the current
# overlays on this interval so a live HA keeps control well inside the overlay
# termination window (``next_block`` is >= one schedule block away). If HA dies
# the overlay lapses and Tado falls back to the baseline. Chosen comfortably
# below any realistic ``next_block`` gap; re-asserting an overlay is local/free.
HEARTBEAT_INTERVAL_MINUTES = 10

# Custom data field read from a referenced ``schedule`` helper's current on-block
# (spec §1.1: "De HA schedule-helper draagt per blok een data.temperature").
SCHEDULE_TEMPERATURE_ATTR = "temperature"

# Dispatcher signal the coordinator sends per zone when its runtime state
# changes; the climate entity and control_source sensor listen on the zone's signal.
SIGNAL_ZONE_UPDATE = "zone_heating_zone_update_{subentry_id}"
# Dispatcher signal for global-state changes (``binary_sensor.heating_in_fallback``).
SIGNAL_GLOBAL_UPDATE = "zone_heating_global_update"

# tado_hijack exposes a per-zone schedule switch; ``on`` = the zone follows its
# Tado schedule, ``off`` = HA holds an overlay (spec §5). Derived from the zone's
# first target climate (``switch.<object_id>_schedule``) unless configured.
SWITCH_DOMAIN = "switch"
SCHEDULE_SWITCH_SUFFIX = "_schedule"

# Store (``.storage``) holding the per-zone signature of the last-written Tado
# baseline, so a reload/restart only rewrites a baseline that actually changed
# (spec §8 dedupe; avoids needless Tado-cloud calls on every reload).
STORAGE_VERSION = 1
STORAGE_KEY_BASELINES = "zone_heating_baselines"

# --- climate.<zone> extra_state_attributes (spec §4) ------------------------
ATTR_ZONE_STATE = "zone_state"
ATTR_TARGET_SETPOINT = "target_setpoint"
ATTR_PRESENT = "present"
ATTR_WINDOW_OPEN = "window_open"
ATTR_TARGET_ENTITIES = "target_entities"
ATTR_CONTROL_SOURCE = "control_source"
ATTR_OVERRIDE_ACTIVE = "override_active"
ATTR_OVERRIDE_REMAINING = "override_remaining_min"

# --- Manual control / Custom detection (F3, spec §5) ------------------------
# Shadow-state debounce windows (seconds). An underlying-setpoint change within
# ``OWN_COMMAND_DEBOUNCE`` of HA's own command is HA's echo; a change within
# ``PRESENCE_DEBOUNCE`` of an HA-initiated presence switch is presence-driven.
OWN_COMMAND_DEBOUNCE_SECONDS = 10
PRESENCE_DEBOUNCE_SECONDS = 30

# Truthy raw presence states (entity sources), before debounce (spec §6).
PRESENCE_TRUTHY_STATES = frozenset({"on", "home", "true", "yes", "1"})

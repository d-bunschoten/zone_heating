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
CONF_STANDBY_SCHEDULE = "standby_schedule"
CONF_ACTIVE_SCHEDULE = "active_schedule"

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
DEFAULT_DEAD_MANS_SWITCH = True
DEFAULT_OVERLAY_TERMINATION = OVERLAY_NEXT_BLOCK
DEFAULT_OVERLAY_TIMER_DURATION = 60

# Per-zone setpoint defaults (°C). Spec §3 ordering is away < standby < active
# (away = nobody home, so coolest). The F1 brief's "e.g. 18/15/20" is
# illustrative ("e.g."); we use a monotonic sensible default instead.
DEFAULT_AWAY_TEMP = 16.0
DEFAULT_STANDBY_TEMP = 18.0
DEFAULT_ACTIVE_TEMP = 20.0

# Temperature number bounds (shared by zone setpoints, boost temp, climate).
MIN_TEMP = 5.0
MAX_TEMP = 30.0
TEMP_STEP = 0.5

# Global entity defaults (spec §1.2).
DEFAULT_BOOST_TEMP = 23.0
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
KEY_BOOST_TEMP = "heating_boost_temp"
KEY_BOOST_DURATION = "heating_boost_duration"
KEY_OVERRIDE_TIMEOUT = "heating_override_timeout"
KEY_HOME_PRESENT = "heating_home_present"
KEY_IN_FALLBACK = "heating_in_fallback"

# --- Per-zone entity key suffixes (used for unique_id) -----------------------
KEY_CLIMATE = "climate"
KEY_AWAY_TEMP = "away_temp"
KEY_STANDBY_TEMP = "standby_temp"
KEY_ACTIVE_TEMP = "active_temp"
KEY_CONTROL_SOURCE = "control_source"
KEY_PRESENT = "present"
KEY_WINDOW_OPEN = "window_open"

"""Config flow for the Zone Heating integration.

Pattern (spec §1.0, §1.3; plan §3): a single *hub* config entry carries the
global configuration (options flow) and the global entities. Each zone is a
config **subentry** of type ``zone``; every subentry becomes exactly one HA
device with the per-zone entities attached. Config subentries are the modern HA
pattern for "one hub + N child devices" and are stable on HA 2026.10.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACTIVE_SCHEDULE,
    CONF_ACTIVE_SCHEDULE_GATE,
    CONF_DEAD_MANS_SWITCH,
    CONF_HOME_OFF_DELAY,
    CONF_HOME_ON_DELAY,
    CONF_HOME_PRESENCE_ENTITY,
    CONF_HOME_PRESENCE_TEMPLATE,
    CONF_NAME,
    CONF_OFF_BASELINE_TEMP,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_OVERLAY_TERMINATION,
    CONF_OVERLAY_TIMER_DURATION,
    CONF_PRESENCE_ENTITY,
    CONF_PRESENCE_TEMPLATE,
    CONF_SCHEDULE_SWITCH,
    CONF_STANDBY_SCHEDULE,
    CONF_TARGETS,
    CONF_WINDOW_CLOSE_DELAY,
    CONF_WINDOW_OPEN_DELAY,
    CONF_WINDOW_SENSOR,
    DEFAULT_ACTIVE_SCHEDULE_GATE,
    DEFAULT_DEAD_MANS_SWITCH,
    DEFAULT_HOME_OFF_DELAY,
    DEFAULT_HOME_ON_DELAY,
    DEFAULT_OFF_BASELINE_TEMP,
    DEFAULT_OFF_DELAY,
    DEFAULT_ON_DELAY,
    DEFAULT_OVERLAY_TERMINATION,
    DEFAULT_OVERLAY_TIMER_DURATION,
    DEFAULT_WINDOW_CLOSE_DELAY,
    DEFAULT_WINDOW_OPEN_DELAY,
    DOMAIN,
    MAX_TEMP,
    MIN_TEMP,
    OVERLAY_TERMINATION_OPTIONS,
    SUBENTRY_TYPE_ZONE,
    TEMP_STEP,
)

# A single, unique id so only one hub entry can ever be created.
_SINGLE_INSTANCE_ID = "zone_heating_hub"


def _minutes_selector(mx: int = 600) -> selector.NumberSelector:
    """A number selector expressing a duration in minutes."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=mx,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


def _zone_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for adding/reconfiguring a zone subentry (spec §1.1, §1.3, §6)."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, vol.UNDEFINED)
            ): selector.TextSelector(),
            vol.Required(
                CONF_TARGETS, default=defaults.get(CONF_TARGETS, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Optional(
                CONF_PRESENCE_ENTITY,
                description={"suggested_value": defaults.get(CONF_PRESENCE_ENTITY)},
            ): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Optional(
                CONF_PRESENCE_TEMPLATE,
                description={
                    "suggested_value": defaults.get(CONF_PRESENCE_TEMPLATE)
                },
            ): selector.TemplateSelector(),
            vol.Optional(
                CONF_ON_DELAY, default=defaults.get(CONF_ON_DELAY, DEFAULT_ON_DELAY)
            ): _minutes_selector(),
            vol.Optional(
                CONF_OFF_DELAY,
                default=defaults.get(CONF_OFF_DELAY, DEFAULT_OFF_DELAY),
            ): _minutes_selector(),
            vol.Optional(
                CONF_WINDOW_SENSOR,
                description={"suggested_value": defaults.get(CONF_WINDOW_SENSOR)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional(
                CONF_WINDOW_OPEN_DELAY,
                default=defaults.get(
                    CONF_WINDOW_OPEN_DELAY, DEFAULT_WINDOW_OPEN_DELAY
                ),
            ): _minutes_selector(),
            vol.Optional(
                CONF_WINDOW_CLOSE_DELAY,
                default=defaults.get(
                    CONF_WINDOW_CLOSE_DELAY, DEFAULT_WINDOW_CLOSE_DELAY
                ),
            ): _minutes_selector(),
            vol.Optional(
                CONF_SCHEDULE_SWITCH,
                description={"suggested_value": defaults.get(CONF_SCHEDULE_SWITCH)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Optional(
                CONF_STANDBY_SCHEDULE,
                description={
                    "suggested_value": defaults.get(CONF_STANDBY_SCHEDULE)
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="schedule")
            ),
            vol.Optional(
                CONF_ACTIVE_SCHEDULE,
                description={"suggested_value": defaults.get(CONF_ACTIVE_SCHEDULE)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="schedule")
            ),
            vol.Optional(
                CONF_ACTIVE_SCHEDULE_GATE,
                default=defaults.get(
                    CONF_ACTIVE_SCHEDULE_GATE, DEFAULT_ACTIVE_SCHEDULE_GATE
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_OFF_BASELINE_TEMP,
                default=defaults.get(
                    CONF_OFF_BASELINE_TEMP, DEFAULT_OFF_BASELINE_TEMP
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_TEMP,
                    max=MAX_TEMP,
                    step=TEMP_STEP,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the global hub options flow (spec §1.2, §1.3, §7)."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_HOME_PRESENCE_ENTITY,
                description={
                    "suggested_value": defaults.get(CONF_HOME_PRESENCE_ENTITY)
                },
            ): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Optional(
                CONF_HOME_PRESENCE_TEMPLATE,
                description={
                    "suggested_value": defaults.get(CONF_HOME_PRESENCE_TEMPLATE)
                },
            ): selector.TemplateSelector(),
            vol.Optional(
                CONF_HOME_ON_DELAY,
                default=defaults.get(CONF_HOME_ON_DELAY, DEFAULT_HOME_ON_DELAY),
            ): _minutes_selector(),
            vol.Optional(
                CONF_HOME_OFF_DELAY,
                default=defaults.get(CONF_HOME_OFF_DELAY, DEFAULT_HOME_OFF_DELAY),
            ): _minutes_selector(),
            vol.Optional(
                CONF_DEAD_MANS_SWITCH,
                default=defaults.get(
                    CONF_DEAD_MANS_SWITCH, DEFAULT_DEAD_MANS_SWITCH
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_OVERLAY_TERMINATION,
                default=defaults.get(
                    CONF_OVERLAY_TERMINATION, DEFAULT_OVERLAY_TERMINATION
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=OVERLAY_TERMINATION_OPTIONS,
                    translation_key="overlay_termination",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_OVERLAY_TIMER_DURATION,
                default=defaults.get(
                    CONF_OVERLAY_TIMER_DURATION, DEFAULT_OVERLAY_TIMER_DURATION
                ),
            ): _minutes_selector(),
        }
    )


class ZoneHeatingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zone Heating."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Return the global options flow."""
        return ZoneHeatingOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Register the ``zone`` subentry type."""
        return {SUBENTRY_TYPE_ZONE: ZoneSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step; create a single hub entry."""
        await self.async_set_unique_id(_SINGLE_INSTANCE_ID)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Zone Heating", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))


class ZoneSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a heating zone (one subentry == one device)."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new zone."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME], data=user_input
            )
        return self.async_show_form(step_id="user", data_schema=_zone_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure an existing zone."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=user_input[CONF_NAME],
                data=user_input,
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_zone_schema(dict(subentry.data)),
        )


class ZoneHeatingOptionsFlow(OptionsFlow):
    """Global (hub) options flow (spec §1.2, §1.3)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the global configuration."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
        )

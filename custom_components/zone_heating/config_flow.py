"""Config flow for the Zone Heating integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN

# A single, unique id so only one hub entry can ever be created.
_SINGLE_INSTANCE_ID = "zone_heating_hub"


class ZoneHeatingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zone Heating."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        Shows an empty confirmation form and creates a single hub entry.
        """
        # Allow only one hub instance.
        await self.async_set_unique_id(_SINGLE_INSTANCE_ID)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Zone Heating", data={})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )

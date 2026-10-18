"""Tado (tado_hijack) backend for Zone Heating (plan §4, spec §4).

Production backend: setpoints are written via ``tado_hijack.set_mode`` with
``hvac_mode=heat`` + ``temperature`` on the zone's target entities; turning a
zone off calls the same service with ``hvac_mode=off``.

Overlay termination / dead-man's-switch (the ``overlay`` parameter) is **F4**;
F2 keeps the call minimal. The service domain/name live in constants so tests can
mock the service (``async_mock_service``).
"""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant

from .backend import HeatingBackend

# Kept as constants so tests can target the exact service to mock.
TADO_DOMAIN = "tado_hijack"
TADO_SERVICE_SET_MODE = "set_mode"

ATTR_HVAC_MODE = "hvac_mode"
TADO_HVAC_HEAT = "heat"
TADO_HVAC_OFF = "off"


class TadoBackend(HeatingBackend):
    """Drives tado_hijack climate entities via ``tado_hijack.set_mode``."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def set_setpoint(
        self, targets: list[str], temperature: float
    ) -> None:
        """Write a heat setpoint to the targets (one call for all targets)."""
        if not targets:
            return
        await self._hass.services.async_call(
            TADO_DOMAIN,
            TADO_SERVICE_SET_MODE,
            {
                ATTR_ENTITY_ID: targets,
                ATTR_HVAC_MODE: TADO_HVAC_HEAT,
                ATTR_TEMPERATURE: temperature,
            },
            blocking=True,
        )

    async def set_off(self, targets: list[str]) -> None:
        """Turn the targets off."""
        if not targets:
            return
        await self._hass.services.async_call(
            TADO_DOMAIN,
            TADO_SERVICE_SET_MODE,
            {
                ATTR_ENTITY_ID: targets,
                ATTR_HVAC_MODE: TADO_HVAC_OFF,
            },
            blocking=True,
        )

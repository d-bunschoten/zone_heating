"""Generic (non-Tado) backend stub for Zone Heating (plan §4).

Reusability naad: drives plain ``climate`` entities via ``climate.set_temperature``
/ ``climate.set_hvac_mode``. There is no overlay termination here, so the
dead-man's-switch does not apply (the ``termination`` argument is ignored) and there
is no Tado-style baseline timetable (``set_schedule`` is a no-op). This stub is
intentionally **not wired** yet (testing targets the Tado backend, plan §7); it
exists so the abstraction has a second implementation and the extension point is
proven.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant

from .backend import HeatingBackend, OverlayTermination
from .const import LOGGER

ATTR_HVAC_MODE = "hvac_mode"


class GenericBackend(HeatingBackend):
    """Drives standard climate entities (not wired in F2)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def set_setpoint(
        self,
        targets: list[str],
        temperature: float,
        termination: OverlayTermination,
    ) -> None:
        if not targets:
            return
        await self._hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: targets, ATTR_TEMPERATURE: temperature},
            blocking=True,
        )

    async def set_off(
        self, targets: list[str], termination: OverlayTermination
    ) -> None:
        if not targets:
            return
        await self._hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: targets, ATTR_HVAC_MODE: HVACMode.OFF},
            blocking=True,
        )

    async def set_schedule(
        self,
        targets: list[str],
        timetable: str,
        blocks: list[dict[str, object]],
    ) -> None:
        # Plain climate entities have no writable baseline timetable; the baseline
        # is a Tado concept. Nothing to do here.
        LOGGER.debug("GenericBackend.set_schedule is a no-op (no Tado timetable)")

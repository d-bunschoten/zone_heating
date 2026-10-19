"""Tado (tado_hijack) backend for Zone Heating (plan §4, spec §4, §7, §8).

Production backend:

- setpoints are written via ``tado_hijack.set_mode`` with ``hvac_mode=heat`` +
  ``temperature`` on the zone's target entities; turning a zone off calls the same
  service with ``hvac_mode=off``. Both carry the configured **overlay termination**
  (the dead-man's-switch mechanism, spec §7): ``next_block`` ends the overlay at the
  next schedule block; ``timer`` passes a ``duration`` so tado_hijack uses a timed
  overlay. A finite termination means a dead HA eventually falls back to the Tado
  baseline.
- baselines (normal / off) are written via ``tado_hijack.set_schedule`` (spec §8),
  one call per zone, with ``activate`` so the written timetable becomes active.

Service domains/names + field names live in constants so tests can mock the exact
service (``async_mock_service``) and assert the payload.
"""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant

from .backend import HeatingBackend, OverlayTermination
from .const import OVERLAY_TIMER

# Kept as constants so tests can target the exact service to mock.
TADO_DOMAIN = "tado_hijack"
TADO_SERVICE_SET_MODE = "set_mode"
TADO_SERVICE_SET_SCHEDULE = "set_schedule"

ATTR_HVAC_MODE = "hvac_mode"
ATTR_OVERLAY = "overlay"
ATTR_DURATION = "duration"
ATTR_TIMETABLE = "timetable"
ATTR_ALL_DAYS = "all_days"
ATTR_DAYS = "days"
ATTR_BLOCKS = "blocks"
ATTR_ACTIVATE = "activate"

TADO_HVAC_HEAT = "heat"
TADO_HVAC_OFF = "off"
# tado_hijack overlay select value; a passed ``duration`` instead yields a timer.
TADO_OVERLAY_NEXT_BLOCK = "next_block"


class TadoBackend(HeatingBackend):
    """Drives tado_hijack climate entities via ``tado_hijack.set_mode``."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    @staticmethod
    def _termination_fields(termination: OverlayTermination) -> dict[str, object]:
        """Map an :class:`OverlayTermination` onto set_mode fields (spec §7).

        ``timer`` with a duration -> ``duration`` (tado_hijack derives a timed
        overlay); otherwise ``overlay=next_block`` (overlay ends at the next block).
        """
        if termination.mode == OVERLAY_TIMER and termination.duration_min:
            return {ATTR_DURATION: int(termination.duration_min)}
        return {ATTR_OVERLAY: TADO_OVERLAY_NEXT_BLOCK}

    async def set_setpoint(
        self,
        targets: list[str],
        temperature: float,
        termination: OverlayTermination,
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
                **self._termination_fields(termination),
            },
            blocking=True,
        )

    async def set_off(
        self, targets: list[str], termination: OverlayTermination
    ) -> None:
        """Turn the targets off (overlay termination still applies)."""
        if not targets:
            return
        await self._hass.services.async_call(
            TADO_DOMAIN,
            TADO_SERVICE_SET_MODE,
            {
                ATTR_ENTITY_ID: targets,
                ATTR_HVAC_MODE: TADO_HVAC_OFF,
                **self._termination_fields(termination),
            },
            blocking=True,
        )

    async def set_schedule(
        self,
        targets: list[str],
        timetable: str,
        blocks: list[dict[str, object]],
        *,
        all_days: bool = True,
        days: list[str] | None = None,
    ) -> None:
        """Write one baseline timetable write for the zone (spec §8).

        ``all_days`` True -> ``one_day`` for every weekday; otherwise a ``seven_day``
        write carrying the weekday names in ``days`` (tado_hijack accepts "monday"..
        "sunday"). One call maps to one distinct day-pattern.
        """
        if not targets:
            return
        data: dict[str, object] = {
            ATTR_ENTITY_ID: targets,
            ATTR_TIMETABLE: timetable,
            ATTR_ALL_DAYS: all_days,
            ATTR_BLOCKS: blocks,
            ATTR_ACTIVATE: True,
        }
        if not all_days and days:
            data[ATTR_DAYS] = days
        await self._hass.services.async_call(
            TADO_DOMAIN,
            TADO_SERVICE_SET_SCHEDULE,
            data,
            blocking=True,
        )

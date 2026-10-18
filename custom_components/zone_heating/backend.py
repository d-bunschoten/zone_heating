"""Backend abstraction for Zone Heating (plan §4).

The engine only produces a *desired setpoint / stand*; a backend translates that
into concrete service calls on the underlying climate entities. The production
backend is :mod:`backend_tado` (tado_hijack); :mod:`backend_generic` is a thin
non-Tado stub kept for reusability (not wired yet).

F4 adds the **overlay termination** (the dead-man's-switch mechanism, spec §7):
every setpoint/off write carries a finite termination so a dead HA eventually lets
Tado fall back to the baseline. It also adds :meth:`set_schedule` for the derived
normal / off baselines (spec §8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .const import OVERLAY_NEXT_BLOCK


@dataclass(frozen=True)
class OverlayTermination:
    """How a written overlay expires (spec §7).

    ``mode`` is :data:`~.const.OVERLAY_NEXT_BLOCK` (ends at the next schedule block)
    or :data:`~.const.OVERLAY_TIMER` (ends after ``duration_min`` minutes). Both are
    *finite*: the overlay lapses on its own, which is what makes HA's silence hand
    control back to the Tado baseline. The heartbeat (coordinator) keeps refreshing
    it while HA is alive when the dead-man's-switch is on.
    """

    mode: str = OVERLAY_NEXT_BLOCK
    duration_min: int | None = None


class HeatingBackend(ABC):
    """Translates engine decisions into device commands for a zone's targets."""

    @abstractmethod
    async def set_setpoint(
        self,
        targets: list[str],
        temperature: float,
        termination: OverlayTermination,
    ) -> None:
        """Drive ``targets`` to ``temperature`` (heat) with an overlay termination."""

    @abstractmethod
    async def set_off(
        self, targets: list[str], termination: OverlayTermination
    ) -> None:
        """Turn ``targets`` off with an overlay termination."""

    @abstractmethod
    async def set_schedule(
        self,
        targets: list[str],
        timetable: str,
        blocks: list[dict[str, object]],
    ) -> None:
        """Write a baseline Tado timetable for the zone (1 call/zone, spec §8)."""

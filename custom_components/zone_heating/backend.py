"""Backend abstraction for Zone Heating (plan §4).

The engine only produces a *desired setpoint / stand*; a backend translates that
into concrete service calls on the underlying climate entities. The production
backend is :mod:`backend_tado` (tado_hijack); :mod:`backend_generic` is a thin
non-Tado stub kept for reusability (not wired yet).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class HeatingBackend(ABC):
    """Translates engine decisions into device commands for a zone's targets."""

    @abstractmethod
    async def set_setpoint(
        self, targets: list[str], temperature: float
    ) -> None:
        """Drive ``targets`` to ``temperature`` (heat)."""

    @abstractmethod
    async def set_off(self, targets: list[str]) -> None:
        """Turn ``targets`` off."""

"""Zone Heating baseline schedule derivation (pure Python, HA-agnostic).

Spec §8. HA writes a *baseline* Tado timetable per zone (1 call/zone) so that,
whenever a zone falls back to its Tado schedule, it runs a sensible curve:

- **Normal baseline** — *derived*, not separately configured. It is built from the
  zone's ``standby_schedule`` (per-block temperatures) with every gap filled with
  the ``standby_temp`` base. That makes a Tado fallback run the very same Standby
  curve HA would otherwise assert, so a fallback is barely noticeable.
- **Off baseline** — a flat frost-protection curve at the zone's configured
  off/frost temperature.

This module is intentionally free of any Home Assistant imports so the derivation
is deterministic and unit-testable in isolation (plan §3).

Why derive explicit full-day ON blocks instead of handing tado_hijack the schedule
helper directly: ``tado_hijack.set_schedule`` fills a schedule-helper's *gaps* with
power **OFF**, whereas the spec wants gaps on the **base temperature**. Producing
explicit, gap-filled ON blocks here is the only way to honour "gaten op de
base-temp" (spec §8).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# tado_hijack ``set_schedule`` timetable types. A single flat block list applied
# to every weekday (all_days=True) is "one_day"; it is the only shape consistent
# with the spec's "1 call/zone" budget (§2, §8). Per-weekday variation in a
# standby schedule is therefore collapsed to one representative daily curve by
# the HA glue before it reaches this module.
TIMETABLE_ONE_DAY = "one_day"

_MINUTES_PER_DAY = 1440


@dataclass(frozen=True)
class ScheduleBlock:
    """One on-block of a standby schedule for a single day.

    ``start``/``end`` are ``"HH:MM"`` or ``"HH:MM:SS"`` strings (an ``end`` of
    ``"00:00"`` / ``"24:00"`` means midnight at the end of the day). ``temperature``
    is the block's setpoint; ``None`` means the block carries no temperature and is
    treated as a gap (filled with the base temperature).
    """

    start: str
    end: str
    temperature: float | None


@dataclass(frozen=True)
class BaselineTimetable:
    """A ``set_schedule``-ready baseline: a timetable type + contiguous blocks.

    ``blocks`` covers the whole day (00:00..24:00) with no gaps; each entry is a
    ``{"start", "end", "temperature"}`` mapping as ``tado_hijack.set_schedule``
    expects in its ``blocks`` field.
    """

    timetable: str
    blocks: tuple[dict[str, object], ...]


def _to_minutes(value: str) -> int:
    """Parse ``"HH:MM"`` / ``"HH:MM:SS"`` to minutes from midnight (24:00 -> 1440)."""
    text = value.strip()
    if text.startswith("24:00"):
        return _MINUTES_PER_DAY
    parts = text.split(":")
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0
    return hours * 60 + minutes


def _fmt(minutes: int) -> str:
    """Format minutes-from-midnight as ``"HH:MM"`` (1440 -> ``"24:00"``)."""
    if minutes >= _MINUTES_PER_DAY:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _span(start: int, end: int, temperature: float) -> dict[str, object]:
    return {"start": _fmt(start), "end": _fmt(end), "temperature": float(temperature)}


def derive_daily_blocks(
    blocks: Iterable[ScheduleBlock], base_temp: float
) -> list[dict[str, object]]:
    """Build one full day of contiguous blocks, gaps filled with ``base_temp``.

    - No on-blocks (or an empty schedule) -> a single flat block at ``base_temp``.
    - On-blocks keep their own temperature; the time before, between and after them
      is filled with ``base_temp``.

    Blocks without a temperature are ignored (they become base-filled gaps).
    Overlapping/odd inputs are clamped defensively; HA's schedule helper already
    guarantees non-overlapping, sorted blocks.
    """
    spans: list[tuple[int, int, float]] = []
    for block in blocks:
        if block.temperature is None:
            continue
        start = _to_minutes(block.start)
        end = _to_minutes(block.end)
        if end == 0 and start > 0:
            end = _MINUTES_PER_DAY
        if end <= start:
            continue
        spans.append((start, end, float(block.temperature)))
    spans.sort(key=lambda item: item[0])

    result: list[dict[str, object]] = []
    cursor = 0
    for start, end, temperature in spans:
        begin = max(start, cursor)
        if begin > cursor:
            result.append(_span(cursor, begin, base_temp))
        if end > begin:
            result.append(_span(begin, end, temperature))
            cursor = end
    if cursor < _MINUTES_PER_DAY:
        result.append(_span(cursor, _MINUTES_PER_DAY, base_temp))
    if not result:
        result.append(_span(0, _MINUTES_PER_DAY, base_temp))
    return result


def derive_normal_baseline(
    day_blocks: Sequence[ScheduleBlock], base_temp: float
) -> BaselineTimetable:
    """Derive the normal baseline timetable from a day's standby blocks (spec §8)."""
    return BaselineTimetable(
        timetable=TIMETABLE_ONE_DAY,
        blocks=tuple(derive_daily_blocks(day_blocks, base_temp)),
    )


def off_baseline(off_temp: float) -> BaselineTimetable:
    """Build the flat off/frost-protection baseline timetable (spec §8)."""
    return BaselineTimetable(
        timetable=TIMETABLE_ONE_DAY,
        blocks=(_span(0, _MINUTES_PER_DAY, off_temp),),
    )

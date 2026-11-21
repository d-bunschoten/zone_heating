"""Zone Heating baseline schedule derivation (pure Python, HA-agnostic).

Spec §8. HA writes a *baseline* Tado timetable per zone so that, whenever a zone
falls back to its Tado schedule, it runs a sensible curve:

- **Normal baseline** — *derived*, not separately configured. It is built from the
  zone's ``standby_schedule`` (per-block temperatures) with every gap filled with
  the ``standby_temp`` base. That makes a Tado fallback run the very same Standby
  curve HA would otherwise assert, so a fallback is barely noticeable.
- **Off baseline** — a flat frost-protection curve at the zone's configured
  off/frost temperature.

**Baseline in the schedule's natural shape — no flattening to one day (spec §8).**
``tado_hijack.set_schedule`` costs **1 cloud-call per dayType** (verified: its
coordinator loops ``for day_type in day_types``, each a queued write). So the
derivation keeps the week's own shape:

- a standby schedule **identical on all weekdays** -> one ``one_day`` timetable
  (``all_days=True``) = 1 call/zone;
- a standby schedule that **varies per day** -> a ``seven_day`` timetable with
  **one write per distinct day-pattern** (identical days grouped; at most 7).

Baseline writes are rare (only on Off-transitions / standby-config changes), so up
to 7 calls/zone is well within the budget (spec §8). This module is intentionally
free of any Home Assistant imports so the derivation is deterministic and
unit-testable in isolation (plan §3).

Why derive explicit full-day ON blocks instead of handing tado_hijack the schedule
helper directly: ``tado_hijack.set_schedule`` fills a schedule-helper's *gaps* with
power **OFF**, whereas the spec wants gaps on the **base temperature**. Producing
explicit, gap-filled ON blocks here is the only way to honour "gaten op de
base-temp" (spec §8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# tado_hijack ``set_schedule`` timetable types. A single flat block list applied to
# every weekday (``all_days=True``) is "one_day"; a per-weekday schedule is written
# as "seven_day" with one write per distinct day-pattern (spec §8).
TIMETABLE_ONE_DAY = "one_day"
TIMETABLE_SEVEN_DAY = "seven_day"

_MINUTES_PER_DAY = 1440

# A single full-day block has no "next schedule block", so Tado rejects a
# NEXT_TIME_BLOCK overlay on it (HTTP 422). We therefore split any flat full-day
# curve into two identical blocks at a deep-night boundary: functionally identical
# heating, but it gives the timetable a transition so NEXT_TIME_BLOCK stays valid
# and overlays never need a (silently-expiring) timer termination (spec §7/§8).
# The boundary sits in the deep night so a zone that lapses onto it there is
# normally in Standby already (no visible change).
_FLAT_SPLIT_MINUTE = 3 * 60  # 03:00

# Tado accepts schedule-block times only on a 5-minute grid (visible in the app UI),
# and rejects a block shorter than 15 minutes ("Each block must be at least 15
# minutes."). HA's schedule helper cannot store a block ending at 00:00/24:00, so a
# "run until midnight" block is kept as ``..->23:59``; left as-is that leaves a
# 1-minute tail gap (00:00-24:00 fill) that Tado refuses, so the whole baseline
# write fails and the zone stays on its last (often frost/off) Tado schedule.
# We therefore snap every derived boundary to the 5-minute grid (23:59 -> 24:00,
# the gap vanishes) and, as a safety net for odd schedules, merge any block that is
# still shorter than the minimum into its neighbour (spec §8).
_TADO_GRANULARITY_MIN = 5
_MIN_BLOCK_MINUTES = 15


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
class BaselineWrite:
    """One ``set_schedule`` write within a :class:`BaselinePlan`.

    ``all_days`` True -> applies to every weekday (``weekdays`` is empty); otherwise
    ``weekdays`` lists the HA weekday names (``"monday".."sunday"``) that share this
    day-pattern. ``blocks`` covers the whole day (00:00..24:00) with no gaps; each
    entry is a ``{"start", "end", "temperature"}`` mapping as
    ``tado_hijack.set_schedule`` expects in its ``blocks`` field.
    """

    all_days: bool
    weekdays: tuple[str, ...]
    blocks: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class BaselinePlan:
    """A full baseline for a zone: a timetable type + one or more writes.

    A ``one_day`` plan has a single ``all_days`` write; a ``seven_day`` plan has one
    write per distinct day-pattern. The coordinator issues one
    ``backend.set_schedule`` call per write (spec §8).
    """

    timetable: str
    writes: tuple[BaselineWrite, ...]


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


def _round_to_grid(minutes: int) -> int:
    """Round a minutes-from-midnight value to the nearest Tado 5-minute boundary.

    23:59 (1439) -> 1440 (24:00), 08:02 -> 08:00, 08:03 -> 08:05. Never exceeds the
    end of the day (1440 is already on the grid).
    """
    rounded = ((minutes + _TADO_GRANULARITY_MIN // 2) // _TADO_GRANULARITY_MIN) * (
        _TADO_GRANULARITY_MIN
    )
    return min(rounded, _MINUTES_PER_DAY)


def _merge_short_blocks(
    blocks: list[dict[str, object]], min_minutes: int
) -> list[dict[str, object]]:
    """Merge any block shorter than ``min_minutes`` into an adjacent one (spec §8).

    Safety net for odd schedules that still produce a sub-minimum block after the
    5-minute snap: a too-short block is absorbed by its previous neighbour (whose
    setpoint then covers the span), or by the next one when it is the first block, so
    the day stays contiguous and Tado never sees a block below its 15-minute floor. A
    lone full-day block is returned unchanged (it is already valid).
    """
    merged = [dict(block) for block in blocks]
    while len(merged) > 1:
        idx = next(
            (
                i
                for i, b in enumerate(merged)
                if _to_minutes(b["end"]) - _to_minutes(b["start"]) < min_minutes  # type: ignore[arg-type]
            ),
            None,
        )
        if idx is None:
            break
        if idx == 0:
            merged[1]["start"] = merged[0]["start"]
            del merged[0]
        else:
            merged[idx - 1]["end"] = merged[idx]["end"]
            del merged[idx]
    return merged


def _ensure_transition(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Guarantee a timetable transition (a "next block") for NEXT_TIME_BLOCK.

    A single block spanning the whole day has no next block; split it into two
    identical blocks at :data:`_FLAT_SPLIT_MINUTE`. Anything with >= 2 blocks (a
    real transition) is returned unchanged.
    """
    if len(blocks) != 1:
        return blocks
    temperature = blocks[0]["temperature"]
    return [
        _span(0, _FLAT_SPLIT_MINUTE, temperature),  # type: ignore[arg-type]
        _span(_FLAT_SPLIT_MINUTE, _MINUTES_PER_DAY, temperature),  # type: ignore[arg-type]
    ]


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
        # Snap to Tado's 5-minute grid so a "until midnight" block stored as 23:59
        # becomes 24:00 (no 1-minute tail gap) and odd times are Tado-valid.
        start = _round_to_grid(start)
        end = _round_to_grid(end)
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
    result = _merge_short_blocks(result, _MIN_BLOCK_MINUTES)
    return _ensure_transition(result)


def _blocks_key(blocks: Sequence[dict[str, object]]) -> tuple:
    """A hashable, comparable signature of a day's blocks (for grouping days)."""
    return tuple(
        (block["start"], block["end"], block["temperature"]) for block in blocks
    )


def derive_normal_baseline(
    day_blocks: Sequence[ScheduleBlock], base_temp: float
) -> BaselinePlan:
    """Derive a one_day normal baseline from a single day's standby blocks (spec §8).

    Kept for the uniform-week case; :func:`derive_normal_baseline_week` is the
    per-weekday entry point the coordinator uses.
    """
    blocks = tuple(derive_daily_blocks(day_blocks, base_temp))
    return BaselinePlan(
        timetable=TIMETABLE_ONE_DAY,
        writes=(BaselineWrite(all_days=True, weekdays=(), blocks=blocks),),
    )


def derive_normal_baseline_week(
    week: Mapping[str, Sequence[ScheduleBlock]],
    base_temp: float,
    weekday_order: Sequence[str],
) -> BaselinePlan:
    """Derive the normal baseline in the week's own shape (spec §8).

    ``week`` maps HA weekday names to that day's standby on-blocks (a missing or
    empty day -> a flat ``base_temp`` day). ``weekday_order`` fixes the canonical
    monday..sunday order. When all days derive to the same curve the result is a
    single ``one_day`` ``all_days`` write; otherwise it is a ``seven_day`` plan with
    one write per distinct day-pattern (identical days grouped, at most 7).
    """
    # (blocks-key, blocks, [weekdays]) groups, in first-seen weekday order.
    groups: list[tuple[tuple, tuple[dict[str, object], ...], list[str]]] = []
    for day in weekday_order:
        day_blocks = week.get(day) or []
        blocks = tuple(derive_daily_blocks(day_blocks, base_temp))
        key = _blocks_key(blocks)
        for existing_key, _blocks, days in groups:
            if existing_key == key:
                days.append(day)
                break
        else:
            groups.append((key, blocks, [day]))

    if len(groups) <= 1:
        blocks = groups[0][1] if groups else (_span(0, _MINUTES_PER_DAY, base_temp),)
        return BaselinePlan(
            timetable=TIMETABLE_ONE_DAY,
            writes=(BaselineWrite(all_days=True, weekdays=(), blocks=blocks),),
        )

    writes = tuple(
        BaselineWrite(all_days=False, weekdays=tuple(days), blocks=blocks)
        for _key, blocks, days in groups
    )
    return BaselinePlan(timetable=TIMETABLE_SEVEN_DAY, writes=writes)


def off_baseline(off_temp: float) -> BaselinePlan:
    """Build the off/frost-protection baseline plan (one_day, 1 write; spec §8).

    Split at the night boundary so even this flat curve keeps a transition and a
    NEXT_TIME_BLOCK overlay stays valid on it (spec §7).
    """
    blocks = tuple(_ensure_transition([_span(0, _MINUTES_PER_DAY, off_temp)]))
    return BaselinePlan(
        timetable=TIMETABLE_ONE_DAY,
        writes=(BaselineWrite(all_days=True, weekdays=(), blocks=blocks),),
    )


def has_next_block(plan: BaselinePlan) -> bool:
    """Whether the baseline contains a schedule transition (a "next block").

    Tado rejects a ``NEXT_TIME_BLOCK`` overlay termination on a zone whose active
    timetable is a single flat block — it has no "next schedule block" and returns
    HTTP 422 ``unsupportedOverlayTerminationType`` (observed live on a flat ONE_DAY
    baseline). A plan has a next block when any single day carries two or more
    blocks, or when the week splits into two or more day-patterns (a ``seven_day``
    plan, whose day boundaries are themselves transitions). A flat ONE_DAY plan with
    one block — which every :func:`off_baseline`, and any standby schedule with no
    on-blocks, produces — has none. The coordinator uses this to pick a timer
    termination for such zones instead (spec §7/§8).
    """
    if len(plan.writes) >= 2:
        return True
    return any(len(write.blocks) >= 2 for write in plan.writes)

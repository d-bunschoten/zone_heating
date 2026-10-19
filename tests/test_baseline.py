"""Pure-engine unit tests for the baseline derivation (spec §8).

Fast and HA-free: these import only ``engine.baseline``. They cover the two
required cases — an empty schedule collapses to a flat base curve, and a schedule
with on-blocks keeps those blocks with every gap filled on the base temperature —
plus the off baseline and a few edge cases (block at the start/end of the day,
multiple blocks, blocks without a temperature).
"""

from __future__ import annotations

from custom_components.zone_heating.engine.baseline import (
    TIMETABLE_ONE_DAY,
    TIMETABLE_SEVEN_DAY,
    ScheduleBlock,
    derive_daily_blocks,
    derive_normal_baseline,
    derive_normal_baseline_week,
    off_baseline,
)

BASE = 18.0

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _b(start: str, end: str, temp: float | None) -> ScheduleBlock:
    return ScheduleBlock(start=start, end=end, temperature=temp)


# --- empty schedule -> flat base --------------------------------------------
def test_empty_schedule_is_flat_base() -> None:
    assert derive_daily_blocks([], BASE) == [
        {"start": "00:00", "end": "24:00", "temperature": BASE}
    ]


def test_blocks_without_temperature_are_gaps() -> None:
    """An on-block with no temperature is treated as a base-filled gap."""
    assert derive_daily_blocks([_b("06:00", "08:00", None)], BASE) == [
        {"start": "00:00", "end": "24:00", "temperature": BASE}
    ]


# --- schedule with blocks -> blocks + base-filled gaps -----------------------
def test_single_mid_day_block_fills_gaps() -> None:
    result = derive_daily_blocks([_b("06:00", "08:00", 21.0)], BASE)
    assert result == [
        {"start": "00:00", "end": "06:00", "temperature": BASE},
        {"start": "06:00", "end": "08:00", "temperature": 21.0},
        {"start": "08:00", "end": "24:00", "temperature": BASE},
    ]


def test_block_at_start_of_day() -> None:
    result = derive_daily_blocks([_b("00:00", "07:00", 20.0)], BASE)
    assert result == [
        {"start": "00:00", "end": "07:00", "temperature": 20.0},
        {"start": "07:00", "end": "24:00", "temperature": BASE},
    ]


def test_block_ending_at_midnight() -> None:
    """A block whose ``to`` is 00:00 means end-of-day (24:00)."""
    result = derive_daily_blocks([_b("22:00", "00:00", 19.0)], BASE)
    assert result == [
        {"start": "00:00", "end": "22:00", "temperature": BASE},
        {"start": "22:00", "end": "24:00", "temperature": 19.0},
    ]


def test_multiple_blocks_with_gaps() -> None:
    result = derive_daily_blocks(
        [_b("06:00:00", "09:00:00", 21.0), _b("17:00:00", "23:00:00", 20.5)],
        BASE,
    )
    assert result == [
        {"start": "00:00", "end": "06:00", "temperature": BASE},
        {"start": "06:00", "end": "09:00", "temperature": 21.0},
        {"start": "09:00", "end": "17:00", "temperature": BASE},
        {"start": "17:00", "end": "23:00", "temperature": 20.5},
        {"start": "23:00", "end": "24:00", "temperature": BASE},
    ]


def test_unsorted_input_is_ordered() -> None:
    result = derive_daily_blocks(
        [_b("17:00", "19:00", 20.0), _b("06:00", "08:00", 21.0)], BASE
    )
    starts = [block["start"] for block in result]
    assert starts == ["00:00", "06:00", "08:00", "17:00", "19:00"]


def test_full_day_block_has_no_base_fill() -> None:
    result = derive_daily_blocks([_b("00:00", "24:00", 20.0)], BASE)
    assert result == [{"start": "00:00", "end": "24:00", "temperature": 20.0}]


# --- single-day wrapper (one_day plan) ---------------------------------------
def test_derive_normal_baseline_wraps_one_day() -> None:
    plan = derive_normal_baseline([_b("06:00", "08:00", 21.0)], BASE)
    assert plan.timetable == TIMETABLE_ONE_DAY
    assert len(plan.writes) == 1
    write = plan.writes[0]
    assert write.all_days is True
    assert write.weekdays == ()
    assert write.blocks == (
        {"start": "00:00", "end": "06:00", "temperature": BASE},
        {"start": "06:00", "end": "08:00", "temperature": 21.0},
        {"start": "08:00", "end": "24:00", "temperature": BASE},
    )


def test_off_baseline_is_flat_frost_temp() -> None:
    plan = off_baseline(7.0)
    assert plan.timetable == TIMETABLE_ONE_DAY
    assert len(plan.writes) == 1
    assert plan.writes[0].all_days is True
    assert plan.writes[0].blocks == (
        {"start": "00:00", "end": "24:00", "temperature": 7.0},
    )


# --- full-week plan (natural schedule shape, spec §8) ------------------------
def test_uniform_week_is_one_day_single_write() -> None:
    """A schedule identical on every weekday -> one_day, 1 all_days write."""
    week = {day: [_b("06:00", "08:00", 21.0)] for day in _WEEKDAYS}
    plan = derive_normal_baseline_week(week, BASE, _WEEKDAYS)
    assert plan.timetable == TIMETABLE_ONE_DAY
    assert len(plan.writes) == 1
    write = plan.writes[0]
    assert write.all_days is True
    assert write.weekdays == ()
    assert write.blocks == (
        {"start": "00:00", "end": "06:00", "temperature": BASE},
        {"start": "06:00", "end": "08:00", "temperature": 21.0},
        {"start": "08:00", "end": "24:00", "temperature": BASE},
    )


def test_no_schedule_is_flat_base_one_day() -> None:
    """An empty week (no blocks on any day) -> flat base one_day, 1 write."""
    week: dict[str, list[ScheduleBlock]] = {day: [] for day in _WEEKDAYS}
    plan = derive_normal_baseline_week(week, BASE, _WEEKDAYS)
    assert plan.timetable == TIMETABLE_ONE_DAY
    assert len(plan.writes) == 1
    assert plan.writes[0].all_days is True
    assert plan.writes[0].blocks == (
        {"start": "00:00", "end": "24:00", "temperature": BASE},
    )


def test_missing_days_default_to_flat_base() -> None:
    """Days absent from the mapping are treated as a flat base day."""
    # Only weekdays carry a block; the weekend is omitted entirely.
    week = {day: [_b("06:00", "08:00", 21.0)] for day in _WEEKDAYS[:5]}
    plan = derive_normal_baseline_week(week, BASE, _WEEKDAYS)
    assert plan.timetable == TIMETABLE_SEVEN_DAY
    assert len(plan.writes) == 2
    weekday_write = next(w for w in plan.writes if "monday" in w.weekdays)
    weekend_write = next(w for w in plan.writes if "saturday" in w.weekdays)
    assert weekday_write.weekdays == ("monday", "tuesday", "wednesday",
                                      "thursday", "friday")
    assert weekend_write.weekdays == ("saturday", "sunday")
    assert weekend_write.blocks == (
        {"start": "00:00", "end": "24:00", "temperature": BASE},
    )


def test_weekday_weekend_split_is_seven_day_two_writes() -> None:
    """Weekdays vs weekend differ -> seven_day with two grouped writes."""
    week: dict[str, list[ScheduleBlock]] = {}
    for day in _WEEKDAYS[:5]:
        week[day] = [_b("06:00", "08:00", 21.0)]
    for day in _WEEKDAYS[5:]:
        week[day] = [_b("09:00", "11:00", 20.0)]
    plan = derive_normal_baseline_week(week, BASE, _WEEKDAYS)
    assert plan.timetable == TIMETABLE_SEVEN_DAY
    assert len(plan.writes) == 2
    for write in plan.writes:
        assert write.all_days is False
    weekday_write = next(w for w in plan.writes if "monday" in w.weekdays)
    weekend_write = next(w for w in plan.writes if "saturday" in w.weekdays)
    assert weekday_write.weekdays == ("monday", "tuesday", "wednesday",
                                      "thursday", "friday")
    assert weekend_write.weekdays == ("saturday", "sunday")
    assert weekday_write.blocks[1] == {
        "start": "06:00", "end": "08:00", "temperature": 21.0,
    }
    assert weekend_write.blocks[1] == {
        "start": "09:00", "end": "11:00", "temperature": 20.0,
    }


def test_seven_distinct_days_is_seven_writes() -> None:
    """Seven different day-patterns -> seven_day with 7 writes, one per day."""
    week = {
        day: [_b("06:00", "08:00", 20.0 + index)]
        for index, day in enumerate(_WEEKDAYS)
    }
    plan = derive_normal_baseline_week(week, BASE, _WEEKDAYS)
    assert plan.timetable == TIMETABLE_SEVEN_DAY
    assert len(plan.writes) == 7
    # Each write covers exactly one weekday, in canonical order.
    assert [w.weekdays for w in plan.writes] == [(day,) for day in _WEEKDAYS]
    assert all(w.all_days is False for w in plan.writes)

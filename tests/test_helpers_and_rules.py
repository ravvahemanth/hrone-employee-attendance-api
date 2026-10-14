"""
Unit tests for business rules (R1 - R10) and helper functions.
"""
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
import pytest

from app.main import (
    IST,
    round_half_up,
    epoch_ms_to_dt,
    dt_to_epoch_ms,
    is_overnight_shift,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    count_working_days,
)


def test_round_half_up():
    """R8 - Half-up rounding vs Python banker's rounding."""
    # Banker's rounding round(2.125, 2) in Python produces 2.12; half-up must produce 2.13
    assert round_half_up(2.125, 2) == 2.13
    assert round_half_up(2.135, 2) == 2.14
    assert round_half_up(8.425, 2) == 8.43
    assert round_half_up(8.435, 2) == 8.44
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(None) is None


def test_epoch_truncation_to_whole_seconds():
    """R1 - Instants truncated to whole seconds."""
    # 1783312500999 ms -> truncated to 1783312500000 ms
    dt = epoch_ms_to_dt(1783312500999)
    assert dt.microsecond == 0
    assert dt_to_epoch_ms(dt) == 1783312500000


def test_is_overnight_shift():
    """R1 - Overnight shift detection."""
    assert is_overnight_shift("22:00", "06:00") is True
    assert is_overnight_shift("20:00", "20:00") is True  # shift_end <= shift_start
    assert is_overnight_shift("09:30", "18:30") is False


def test_attendance_date_derivation():
    """R1 - Attendance date for regular and overnight shifts."""
    # Regular shift: 09:30 - 18:30
    p_in_regular = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)  # 09:30 IST
    assert get_attendance_date(p_in_regular, "09:30", "18:30") == "2026-07-06"

    # Overnight shift: 22:00 - 06:00
    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    p_in_night1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(p_in_night1, "22:00", "06:00") == "2026-07-06"

    # Punch in at 01:10 IST on 2026-07-07 (19:40 UTC on 2026-07-06) -> date is 2026-07-06
    p_in_night2 = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    assert get_attendance_date(p_in_night2, "22:00", "06:00") == "2026-07-06"

    # Punch in at 05:59:59 IST on 2026-07-07 (earlier than shift_end 06:00) -> belongs to 2026-07-06
    p_in_night3 = datetime(2026, 7, 7, 0, 29, 59, tzinfo=timezone.utc)
    assert get_attendance_date(p_in_night3, "22:00", "06:00") == "2026-07-06"

    # Punch in at 06:00:00 IST on 2026-07-07 (not earlier than shift_end) -> belongs to 2026-07-07
    p_in_night4 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    assert get_attendance_date(p_in_night4, "22:00", "06:00") == "2026-07-07"


def test_late_arrival_boundaries():
    """R2 - 10-minute grace period and floor minutes."""
    # Scheduled 09:30 on 2026-07-06
    # 09:40:00 -> 0 late minutes (grace boundary)
    t_094000 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(t_094000, "09:30", "2026-07-06") == 0

    # 09:40:01 -> 10 late minutes (strictly more than 10 minutes)
    t_094001 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(t_094001, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45 late minutes
    t_101559 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(t_101559, "09:30", "2026-07-06") == 45

    # Early arrival: 09:25:00 -> 0 late minutes
    t_early = datetime(2026, 7, 6, 9, 25, 0, tzinfo=IST)
    assert compute_late_minutes(t_early, "09:30", "2026-07-06") == 0


def test_overtime_boundaries():
    """R3 - Overtime >= 30 minutes threshold and overnight shifts."""
    # Regular shift end 18:30 on 2026-07-06
    # Punch out at 18:59:59 IST -> 29 minutes elapsed -> 0 overtime
    t_out_29 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(t_out_29, "18:30", "2026-07-06", "09:30") == 0

    # Punch out at 19:00:00 IST -> exactly 30 minutes -> 30 overtime
    t_out_30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(t_out_30, "18:30", "2026-07-06", "09:30") == 30

    # Punch out at 19:40:00 IST -> 70 minutes -> 70 overtime
    t_out_70 = datetime(2026, 7, 6, 19, 40, 0, tzinfo=IST)
    assert compute_overtime(t_out_70, "18:30", "2026-07-06", "09:30") == 70

    # Overnight shift: 22:00 - 06:00 on 2026-07-06 (shift end is 2026-07-07 06:00 IST)
    # Punch out at 2026-07-07 06:40:00 IST -> 40 minutes overtime
    t_night_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(t_night_out, "06:00", "2026-07-06", "22:00") == 40


def test_work_hours_and_half_day():
    """R4 & R5 - Work hours calculation and half day threshold."""
    p_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    # 4 hours 30 minutes = 4.50 hours
    p_out_45 = p_in + timedelta(hours=4, minutes=30)
    wh_45 = compute_work_hours(p_in, p_out_45)
    assert wh_45 == 4.50
    assert compute_half_day(wh_45) is False

    # 4 hours 29 minutes 40 seconds = 16180 seconds -> 4.4944... -> 4.49 hours -> half day = True
    p_out_under = p_in + timedelta(seconds=16180)
    wh_under = compute_work_hours(p_in, p_out_under)
    assert wh_under == 4.49
    assert compute_half_day(wh_under) is True

    # 4 hours 29 minutes 59 seconds = 16199 seconds -> 4.4997... -> 4.50 hours -> half day = False
    p_out_round = p_in + timedelta(seconds=16199)
    wh_round = compute_work_hours(p_in, p_out_round)
    assert wh_round == 4.50
    assert compute_half_day(wh_round) is False


def test_count_working_days():
    """R7 - Working days (Mon-Fri) and mid-month joiner."""
    # July 2026 has 23 weekdays
    assert count_working_days("2026-07", "2026-01-05") == 23

    # Mid-month joiner: joined 2026-07-13 (Monday) -> 15 weekdays left in July
    assert count_working_days("2026-07", "2026-07-13") == 15

    # Joined next month: joined 2026-08-01 -> 0 working days in July
    assert count_working_days("2026-07", "2026-08-01") == 0

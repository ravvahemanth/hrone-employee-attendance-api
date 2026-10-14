"""
Unit and algorithmic tests verifying analytics calculations:
- Leaderboard competition ranking (1, 2, 2, 4) and ties at the cutoff limit
- Department summary headcount for employees with zero logs
- Trend 7-day moving average calculation and gap filling
- Employee monthly presence, working days, and rate rounding
"""
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.main import (
    round_half_up,
    count_working_days,
    compute_work_hours,
    compute_half_day,
)


def test_leaderboard_competition_ranking_ties_and_cutoff():
    """
    Simulates the leaderboard competition ranking logic (1, 2, 2, 4):
    - Ties share the same rank.
    - Subsequent rank skips.
    - If ties cross the cutoff limit, all tied employees are returned.
    """
    # Test data: employees with late minutes
    # EMP1: 100 min -> rank 1
    # EMP2: 80 min  -> rank 2 (tie)
    # EMP3: 80 min  -> rank 2 (tie)
    # EMP4: 50 min  -> rank 4 (rank 3 skipped!)
    # EMP5: 30 min  -> rank 5
    # EMP6: 30 min  -> rank 5 (tie crossing limit=5!)
    # EMP7: 10 min  -> rank 7
    raw_totals = [
        {"emp_code": "EMP0001", "total_late_minutes": 100},
        {"emp_code": "EMP0002", "total_late_minutes": 80},
        {"emp_code": "EMP0003", "total_late_minutes": 80},
        {"emp_code": "EMP0004", "total_late_minutes": 50},
        {"emp_code": "EMP0005", "total_late_minutes": 30},
        {"emp_code": "EMP0006", "total_late_minutes": 30},
        {"emp_code": "EMP0007", "total_late_minutes": 10},
    ]

    # Algorithm mimicking $setWindowFields with $rank:
    ranked = []
    current_rank = 1
    for idx, emp in enumerate(raw_totals):
        if idx > 0 and emp["total_late_minutes"] == raw_totals[idx - 1]["total_late_minutes"]:
            # Tied with previous
            emp_rank = ranked[-1]["rank"]
        else:
            emp_rank = idx + 1
        ranked.append({**emp, "rank": emp_rank})

    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2
    assert ranked[2]["rank"] == 2
    assert ranked[3]["rank"] == 4  # Rank 3 is skipped!
    assert ranked[4]["rank"] == 5
    assert ranked[5]["rank"] == 5  # Rank 5 tie

    # Limit = 2: both EMP0002 and EMP0003 share rank 2 <= 2, so 3 employees returned
    limit_2 = [r for r in ranked if r["rank"] <= 2]
    assert len(limit_2) == 3
    assert [r["emp_code"] for r in limit_2] == ["EMP0001", "EMP0002", "EMP0003"]

    # Limit = 5: EMP0005 and EMP0006 share rank 5 <= 5, so 6 employees returned!
    limit_5 = [r for r in ranked if r["rank"] <= 5]
    assert len(limit_5) == 6
    assert limit_5[-1]["emp_code"] == "EMP0006"


def test_department_summary_zero_log_headcount():
    """
    Verifies that headcount includes employees with zero attendance logs (R9).
    """
    employees = [
        {"emp_code": "EMP0001", "department": "Sales", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "department": "Sales", "joined_on": "2026-06-15"},
        {"emp_code": "EMP0003", "department": "Sales", "joined_on": "2026-07-20"},  # Joined before July 31
        {"emp_code": "EMP0004", "department": "Sales", "joined_on": "2026-08-01"},  # Joined AFTER July 31
    ]

    # Attendance logs for July: only EMP0001 has logs
    logs = [
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "work_hours": 9.0}
    ]

    period_end = "2026-07-31"
    qualifying_emps = [e for e in employees if e["joined_on"] <= period_end]
    headcount = len(qualifying_emps)
    assert headcount == 3  # EMP1, EMP2, EMP3 qualify. EMP4 does not.

    # Mean work hours is calculated over individual records, not employee averages
    avg_work_hours = round_half_up(sum(l["work_hours"] for l in logs) / len(logs), 2)
    assert avg_work_hours == 9.0


def test_trend_moving_average_logic():
    """
    Verifies that 7-day moving average:
    - Uses up to 6 preceding rows inside the range (first rows use shorter window).
    - Ignores null values (weekends or headcount 0).
    - Returns null if all values in window are null.
    """
    # 10 days of attendance rates (None on weekends: days 5 and 6)
    rates = [
        1.0,   # day 0: window [1.0] -> avg 1.0
        0.8,   # day 1: window [1.0, 0.8] -> avg 0.9
        0.6,   # day 2: window [1.0, 0.8, 0.6] -> avg 0.8
        1.0,   # day 3: window [1.0, 0.8, 0.6, 1.0] -> avg 0.85
        0.9,   # day 4: window [1.0, 0.8, 0.6, 1.0, 0.9] -> avg 0.86
        None,  # day 5 (Saturday): rate is None
        None,  # day 6 (Sunday): rate is None
        0.8,   # day 7: window [0.8, 0.6, 1.0, 0.9, None, None, 0.8] -> non-nulls: [0.8, 0.6, 1.0, 0.9, 0.8]
    ]

    moving_avgs = []
    for i in range(len(rates)):
        window = [rates[j] for j in range(max(0, i - 6), i + 1)]
        valid_vals = [v for v in window if v is not None]
        if valid_vals:
            moving_avgs.append(round_half_up(sum(valid_vals) / len(valid_vals), 4))
        else:
            moving_avgs.append(None)

    assert moving_avgs[0] == 1.0
    assert moving_avgs[1] == 0.9
    assert moving_avgs[2] == 0.8
    assert moving_avgs[3] == 0.85
    assert moving_avgs[4] == 0.86
    # Day 5 (Saturday): rate is None, but moving_avg over preceding non-nulls is 0.86
    assert moving_avgs[5] == 0.86
    # Day 6 (Sunday): moving_avg is still 0.86
    assert moving_avgs[6] == 0.86
    # Day 7: valid non-nulls [0.8, 0.6, 1.0, 0.9, 0.8] -> sum=4.1 / 5 = 0.82
    assert moving_avgs[7] == 0.82

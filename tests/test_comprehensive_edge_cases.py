"""
Comprehensive edge-case and boundary verification suite for HROne Attendance API.
Tests R1-R10 rules, exact boundary values, leap days, half-day thresholds,
audit trails, pagination limits, and error scenarios.
"""
import os
import calendar
from datetime import date, datetime, timedelta, timezone
import pytest
from pymongo import MongoClient
from fastapi.testclient import TestClient

from app.main import (
    app,
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
    IST,
)

EDGE_TEST_CODES = [f"EMP9{i:03d}" for i in range(1, 20)]

@pytest.fixture(scope="module")
def edge_mongo_db():
    client = MongoClient("mongodb://localhost:27017", tz_aware=True)
    db = client["attendance_db_test"]
    assert db.name == "attendance_db_test"
    db.employees.delete_many({"emp_code": {"$in": EDGE_TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": EDGE_TEST_CODES}})
    yield db
    db.employees.delete_many({"emp_code": {"$in": EDGE_TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": EDGE_TEST_CODES}})
    client.close()

@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

# --------------------------------------------------------------------------- #
# 1. R1, R2, R3, R4, R5 Mathematical & Boundary Edge Cases
# --------------------------------------------------------------------------- #

def test_late_arrival_exact_boundaries():
    """
    R2 boundary tests:
    Shift start: 09:30 IST. 10-minute grace period ends at 09:40:00.
    - 09:30:00 -> 0 min late
    - 09:39:59 -> 0 min late
    - 09:40:00 -> 0 min late (grace period inclusive: <= 600 seconds)
    - 09:40:01 -> 10 min late (strictly > 600 seconds: 601 // 60 = 10)
    - 09:40:59 -> 10 min late
    - 09:41:00 -> 11 min late
    """
    att_date = "2026-03-10"
    base_dt = datetime(2026, 3, 10, 4, 0, 0, tzinfo=timezone.utc)  # 09:30 IST is 04:00 UTC

    # 09:40:00 IST = 04:10:00 UTC (exactly 10 min after 09:30)
    t_10m = base_dt + timedelta(minutes=10)
    assert compute_late_minutes(t_10m, "09:30", att_date) == 0

    # 09:40:01 IST = 04:10:01 UTC (10 min 1 sec after 09:30)
    t_10m_1s = t_10m + timedelta(seconds=1)
    assert compute_late_minutes(t_10m_1s, "09:30", att_date) == 10

    # 09:41:00 IST = 04:11:00 UTC (11 min after 09:30)
    t_11m = base_dt + timedelta(minutes=11)
    assert compute_late_minutes(t_11m, "09:30", att_date) == 11

    # Early arrival: 09:15:00 IST = 03:45:00 UTC
    t_early = base_dt - timedelta(minutes=15)
    assert compute_late_minutes(t_early, "09:30", att_date) == 0

def test_overtime_exact_boundaries():
    """
    R3 overtime boundaries:
    Shift end: 18:30 IST.
    - 29 min after shift end (18:59:00) -> 0 min overtime (< 30 min)
    - 29 min 59 sec after shift end (18:59:59) -> 0 min overtime (< 30 min)
    - 30 min after shift end (19:00:00) -> 30 min overtime (>= 30 min threshold)
    - 31 min after shift end (19:01:00) -> 31 min overtime
    """
    att_date = "2026-03-10"
    shift_end = "18:30"
    base_end_dt = datetime(2026, 3, 10, 13, 0, 0, tzinfo=timezone.utc)  # 18:30 IST is 13:00 UTC

    # 29 min later
    t_29m = base_end_dt + timedelta(minutes=29)
    assert compute_overtime(t_29m, shift_end, att_date, "09:30") == 0

    # 30 min later
    t_30m = base_end_dt + timedelta(minutes=30)
    assert compute_overtime(t_30m, shift_end, att_date, "09:30") == 30

    # 31 min later
    t_31m = base_end_dt + timedelta(minutes=31)
    assert compute_overtime(t_31m, shift_end, att_date, "09:30") == 31

    # Early departure (before shift end)
    t_before = base_end_dt - timedelta(minutes=10)
    assert compute_overtime(t_before, shift_end, att_date, "09:30") == 0

def test_overnight_overtime_boundaries():
    """
    Overnight shift: 22:00 to 06:00 (ends next day).
    Attendance date: 2026-03-10.
    Shift end: 2026-03-11 06:00 IST = 2026-03-11 00:30 UTC.
    """
    att_date = "2026-03-10"
    shift_start = "22:00"
    shift_end = "06:00"
    end_utc = datetime(2026, 3, 11, 0, 30, 0, tzinfo=timezone.utc)

    # 06:29 IST -> 29 min -> 0 overtime
    t_29 = end_utc + timedelta(minutes=29)
    assert compute_overtime(t_29, shift_end, att_date, shift_start) == 0

    # 06:30 IST -> 30 min -> 30 overtime
    t_30 = end_utc + timedelta(minutes=30)
    assert compute_overtime(t_30, shift_end, att_date, shift_start) == 30

    # 07:15 IST -> 75 min -> 75 overtime
    t_75 = end_utc + timedelta(minutes=75)
    assert compute_overtime(t_75, shift_end, att_date, shift_start) == 75

def test_half_day_exact_thresholds():
    """
    R5 half-day threshold:
    Half-day is True if work_hours < 4.50; False if work_hours >= 4.50.
    """
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(4.51) is False
    assert compute_half_day(0.0) is True
    assert compute_half_day(None) is False

def test_decimal_round_half_up_edge_cases():
    """
    R4 rounding with ROUND_HALF_UP (arithmetic rounding away from zero):
    """
    assert round_half_up(8.425, 2) == 8.43
    assert round_half_up(8.424, 2) == 8.42
    assert round_half_up(8.426, 2) == 8.43
    assert round_half_up(0.005, 2) == 0.01
    assert round_half_up(0.004, 2) == 0.00
    assert round_half_up(None) is None

def test_leap_year_and_working_days_edge_cases():
    """
    R7 working days with Leap Year February 2024 (29 days, 21 Mon-Fri working days)
    and Non-Leap February 2025 (28 days, 20 Mon-Fri working days).
    """
    # 2024 is a leap year. Feb 1 2024 is Thursday. Feb 29 2024 is Thursday.
    # Total weekdays in Feb 2024 = 21.
    assert count_working_days("2024-02", "2024-01-01") == 21

    # 2025 is not a leap year. Feb 1 2025 is Saturday. Feb 28 2025 is Friday.
    # Total weekdays in Feb 2025 = 20.
    assert count_working_days("2025-02", "2024-01-01") == 20

    # Employee joined mid-month: 2026-01-15 (Thursday)
    # January 2026 has 22 total weekdays.
    # Weekdays from Jan 1 to Jan 14 = 10 weekdays.
    # Remaining weekdays Jan 15-31 = 12 weekdays.
    assert count_working_days("2026-01", "2026-01-15") == 12

    # Employee joined on the very last day of month (2026-01-31 is Saturday -> 0 working days)
    assert count_working_days("2026-01", "2026-01-31") == 0

    # Employee joined after the month ended (future joiner) -> 0 working days
    assert count_working_days("2026-01", "2026-02-01") == 0

def test_epoch_helpers():
    """Test helper functions with None and timezone conversion."""
    assert epoch_ms_to_dt(None) is None
    assert dt_to_epoch_ms(None) is None

    # Naive dt gets treated as UTC
    naive = datetime(2026, 1, 1, 0, 0, 0)
    ms = dt_to_epoch_ms(naive)
    assert ms == int(naive.replace(tzinfo=timezone.utc).timestamp() * 1000)

# --------------------------------------------------------------------------- #
# 2. Endpoint Boundary and Edge-Case Tests (FastAPI + MongoDB)
# --------------------------------------------------------------------------- #

def test_employee_creation_and_validation_edge_cases(client, edge_mongo_db):
    """Test edge cases in employee creation."""
    # 1. Invalid date (Feb 30th)
    r1 = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Invalid Date",
        "email": "inv@example.com",
        "department": "IT",
        "joined_on": "2026-02-30"
    })
    assert r1.status_code == 422

    # 2. Invalid email format
    r2 = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Invalid Email",
        "email": "not-an-email",
        "department": "IT",
        "joined_on": "2026-01-01"
    })
    assert r2.status_code == 422

    # 3. Equal shift_start and shift_end
    r3 = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Equal Shifts",
        "email": "equal@example.com",
        "department": "IT",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01"
    })
    assert r3.status_code == 422

    # 4. Valid employee creation
    r4 = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Valid Test User",
        "email": "user9001@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })
    assert r4.status_code == 201
    assert r4.json()["emp_code"] == "EMP9001"

def test_punch_in_and_out_edge_cases(client, edge_mongo_db):
    """Test punch-in and punch-out edge cases."""
    # 1. Punch-in for unknown employee -> 404
    r_unk = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r_unk.status_code == 404

    # 2. Punch-out for unknown employee -> 404
    r_out_unk = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert r_out_unk.status_code == 404

    # 3. Punch-out when no punch-in exists -> 404
    r_no_in = client.post("/attendance/punch-out", json={"emp_code": "EMP9001"})
    assert r_no_in.status_code == 404

    # 4. Punch-in for overnight shift (22:00-06:00) at 01:30 IST on 2026-01-11
    # 2026-01-11 01:30:00 IST is before 06:00 shift end -> belongs to 2026-01-10!
    # 2026-01-11 01:30:00 IST = 2026-01-10 20:00:00 UTC
    p_in_ms = int(datetime(2026, 1, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9001",
        "punched_at": p_in_ms,
        "status": "PRESENT"
    })
    assert r_in.status_code == 201
    assert r_in.json()["date"] == "2026-01-10"

    # 5. Punch-out == punch-in must return 422 (must be strictly after punch_in)
    p_out_equal = p_in_ms
    r_out_inv = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9001",
        "punched_at": p_out_equal
    })
    assert r_out_inv.status_code == 422

    # 6. Punch-out > 24 hours after punch-in must return 422
    p_out_too_late = p_in_ms + (25 * 3600 * 1000)
    r_out_late = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9001",
        "punched_at": p_out_too_late
    })
    assert r_out_late.status_code == 422

    # 7. Valid punch-out at 06:45 IST on 2026-01-11 (45 min overtime)
    # 2026-01-11 06:45:00 IST = 2026-01-11 01:15:00 UTC
    p_out_ms = int(datetime(2026, 1, 11, 1, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9001",
        "punched_at": p_out_ms
    })
    assert r_out.status_code == 200
    out_json = r_out.json()
    assert out_json["work_hours"] == 5.25
    assert out_json["overtime_minutes"] == 45
    assert out_json["half_day"] is False

def test_attendance_list_filtering_and_pagination(client, edge_mongo_db):
    """Test GET /attendance query filtering and pagination."""
    # 1. Invalid date_from > date_to -> 422
    r_bad_date = client.get("/attendance?date_from=2026-01-20&date_to=2026-01-10")
    assert r_bad_date.status_code == 422

    # 2. Invalid page / page_size -> 422
    assert client.get("/attendance?page=0").status_code == 422
    assert client.get("/attendance?page_size=101").status_code == 422

    # 3. Filter by emp_code and status
    r_list = client.get("/attendance?emp_code=EMP9001&status=PRESENT&page=1&page_size=10")
    assert r_list.status_code == 200
    data = r_list.json()
    assert data["total"] >= 1
    assert data["items"][0]["emp_code"] == "EMP9001"
    assert data["items"][0]["status"] == "PRESENT"

def test_regularize_attendance_edge_cases(client, edge_mongo_db):
    """
    Test manual correction / regularization PATCH endpoint edge cases:
    - Setting ABSENT clears punch times and derived fields
    - Providing punch_in with ABSENT returns 422
    - Supplying no changes returns 422
    - Changing punch_in to date that derives different attendance date returns 422
    - History audit trail accumulates correctly
    """
    # 1. Unknown employee -> 404
    r_unk_emp = client.patch("/attendance/EMP9999/2026-01-10", json={
        "status": "ABSENT",
        "reason": "Correcting attendance",
        "regularized_by": "MGR001"
    })
    assert r_unk_emp.status_code == 404

    # 2. Unknown date record -> 404
    r_unk_rec = client.patch("/attendance/EMP9001/2026-05-01", json={
        "status": "ABSENT",
        "reason": "Correcting attendance",
        "regularized_by": "MGR001"
    })
    assert r_unk_rec.status_code == 404

    # 3. No changes in body -> 422
    r_no_chg = client.patch("/attendance/EMP9001/2026-01-10", json={
        "reason": "No fields passed",
        "regularized_by": "MGR001"
    })
    assert r_no_chg.status_code == 422

    # 4. Status ABSENT with punch_in supplied -> 422
    r_abs_in = client.patch("/attendance/EMP9001/2026-01-10", json={
        "status": "ABSENT",
        "punch_in": int(datetime(2026, 1, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "reason": "Supplied punch_in with ABSENT",
        "regularized_by": "MGR001"
    })
    assert r_abs_in.status_code == 422

    # 5. Punch-in derives different attendance date -> 422
    # Overnight shift (22:00-06:00). Punch-in at 14:00 IST on 2026-01-10 derives date 2026-01-10,
    # but punch-in at 23:00 IST on 2026-01-11 derives date 2026-01-11!
    p_diff_date = int(datetime(2026, 1, 11, 17, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_wrong_date = client.patch("/attendance/EMP9001/2026-01-10", json={
        "punch_in": p_diff_date,
        "reason": "Wrong date attempt",
        "regularized_by": "MGR001"
    })
    assert r_wrong_date.status_code == 422

    # 6. Valid regularize: update status to ABSENT
    r_abs = client.patch("/attendance/EMP9001/2026-01-10", json={
        "status": "ABSENT",
        "reason": "Employee called in sick for shift",
        "regularized_by": "MGR001"
    })
    assert r_abs.status_code == 200
    abs_json = r_abs.json()
    assert abs_json["status"] == "ABSENT"
    assert abs_json["punch_in"] is None
    assert abs_json["punch_out"] is None
    assert abs_json["work_hours"] is None
    assert abs_json["late_minutes"] == 0
    assert abs_json["overtime_minutes"] == 0
    assert len(abs_json["history"]) == 1
    assert abs_json["history"][0]["reason"] == "Employee called in sick for shift"

    # 7. Subsequent regularize: change from ABSENT back to WFH with punch times
    p_in_new = int(datetime(2026, 1, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    p_out_new = int(datetime(2026, 1, 11, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_wfh = client.patch("/attendance/EMP9001/2026-01-10", json={
        "status": "WFH",
        "punch_in": p_in_new,
        "punch_out": p_out_new,
        "reason": "Medical slip approved WFH",
        "regularized_by": "MGR002"
    })
    assert r_wfh.status_code == 200
    wfh_json = r_wfh.json()
    assert wfh_json["status"] == "WFH"
    assert wfh_json["punch_in"] == p_in_new
    assert wfh_json["punch_out"] == p_out_new
    assert len(wfh_json["history"]) == 2  # Audit history preserves both updates

def test_analytics_and_admin_explain_edge_cases(client, edge_mongo_db):
    """Test analytics edge cases and admin explain parameter coverage."""
    # 1. Monthly analytics for unknown employee -> 404
    assert client.get("/analytics/employees/EMP9999/monthly?month=2026-01").status_code == 404

    # 2. Monthly analytics for future joiner (0 working days)
    # Create employee joined in Dec 2026
    client.post("/employees", json={
        "emp_code": "EMP9002",
        "name": "Future Joiner",
        "email": "future@example.com",
        "department": "Finance",
        "joined_on": "2026-12-01"
    })
    r_fut = client.get("/analytics/employees/EMP9002/monthly?month=2026-01")
    assert r_fut.status_code == 200
    fut_json = r_fut.json()
    assert fut_json["working_days"] == 0
    assert fut_json["attendance_pct"] is None

    # 3. Explain endpoint validation
    # Missing required month on late_leaderboard -> 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    # Unsupported endpoint name -> 422
    assert client.get("/admin/explain/nonexistent_ep").status_code == 422

    # Explain attendance_list with all query parameters
    r_exp = client.get(
        "/admin/explain/attendance_list?emp_code=EMP9001&date_from=2026-01-01&date_to=2026-01-31&status=PRESENT"
    )
    assert r_exp.status_code == 200
    assert "explain" in r_exp.json()

    # Explain attendance_list with date_from > date_to -> 422
    assert client.get("/admin/explain/attendance_list?date_from=2026-02-01&date_to=2026-01-01").status_code == 422

def test_serialize_employee_date_formats():
    """Test serialize_employee with varied BSON representation formats."""
    from app.main import serialize_employee

    # 1. Integer timestamp
    doc_int = {
        "emp_code": "EMP9010",
        "name": "Int Date",
        "email": "int@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": 1700000000000
    }
    assert serialize_employee(doc_int)["created_at"] == 1700000000000

    # 2. Float timestamp
    doc_float = {
        "emp_code": "EMP9011",
        "name": "Float Date",
        "email": "float@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": 1700000000000.5
    }
    assert serialize_employee(doc_float)["created_at"] == 1700000000000

    # 3. Dict with $date integer
    doc_dict_int = {
        "emp_code": "EMP9012",
        "name": "Dict Date",
        "email": "dict@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": {"$date": 1700000000000}
    }
    assert serialize_employee(doc_dict_int)["created_at"] == 1700000000000

    # 4. Dict with $date ISO string
    doc_dict_iso = {
        "emp_code": "EMP9013",
        "name": "ISO Date",
        "email": "iso@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": {"$date": "2026-01-01T00:00:00Z"}
    }
    assert serialize_employee(doc_dict_iso)["created_at"] == int(
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    )

    # 5. Missing / invalid created_at defaults to 0
    doc_none = {
        "emp_code": "EMP9014",
        "name": "None Date",
        "email": "none@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": None
    }
    assert serialize_employee(doc_none)["created_at"] == 0

def test_punch_in_server_clock_default(client, edge_mongo_db):
    """Test punch_in when punched_at is omitted (uses server clock)."""
    # Create employee EMP9005
    client.post("/employees", json={
        "emp_code": "EMP9005",
        "name": "Clock User",
        "email": "clock@example.com",
        "department": "Admin",
        "joined_on": "2026-01-01"
    })
    # Punch in with no punched_at field
    r = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9005",
        "status": "PRESENT"
    })
    assert r.status_code == 201
    assert "date" in r.json()
    assert r.json()["punch_in"] is not None

def test_regularize_no_effective_changes_and_durations(client, edge_mongo_db):
    """
    Test regularization producing no actual changes (returns 422)
    and invalid durations (> 24 hours or punch_out <= punch_in).
    """
    # Setup employee and punch record
    client.post("/employees", json={
        "emp_code": "EMP9006",
        "name": "Regularize Tester",
        "email": "reg@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    p_in = int(datetime(2026, 1, 12, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    p_out = p_in + (8 * 3600 * 1000)

    client.post("/attendance/punch-in", json={
        "emp_code": "EMP9006",
        "punched_at": p_in,
        "status": "PRESENT"
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP9006",
        "punched_at": p_out
    })

    # 1. Pass identical status and times -> produces no change -> 422
    r_same = client.patch("/attendance/EMP9006/2026-01-12", json={
        "status": "PRESENT",
        "punch_in": p_in,
        "punch_out": p_out,
        "reason": "Identical values passed",
        "regularized_by": "MGR001"
    })
    assert r_same.status_code == 422
    assert "no actual changes" in r_same.json()["detail"].lower()

    # 2. Regularize punch_out <= punch_in -> 422
    r_bad_order = client.patch("/attendance/EMP9006/2026-01-12", json={
        "punch_out": p_in - 1000,
        "reason": "Invalid time order",
        "regularized_by": "MGR001"
    })
    assert r_bad_order.status_code == 422

    # 3. Regularize punch_out > 24 hours -> 422
    r_too_long = client.patch("/attendance/EMP9006/2026-01-12", json={
        "punch_out": p_in + (25 * 3600 * 1000),
        "reason": "Excessive shift length",
        "regularized_by": "MGR001"
    })
    assert r_too_long.status_code == 422

    # 4. Regularize half_day flip: reduce work hours to 3 hours (< 4.50) -> half_day becomes True
    p_out_half = p_in + (3 * 3600 * 1000)
    r_half = client.patch("/attendance/EMP9006/2026-01-12", json={
        "punch_out": p_out_half,
        "reason": "Half day adjustment approved",
        "regularized_by": "MGR001"
    })
    assert r_half.status_code == 200
    assert r_half.json()["half_day"] is True
    diff = r_half.json()["history"][-1]["changes"]
    assert "half_day" in diff
    assert diff["half_day"]["from"] is False
    assert diff["half_day"]["to"] is True

def test_attendance_list_single_date_boundaries(client, edge_mongo_db):
    """Test GET /attendance with single date boundaries (only date_from or only date_to)."""
    r_from = client.get("/attendance?date_from=2026-01-01&page=1&page_size=5")
    assert r_from.status_code == 200

    r_to = client.get("/attendance?date_to=2026-01-31&page=1&page_size=5")
    assert r_to.status_code == 200

def test_regularize_record_with_no_punch_out(client, edge_mongo_db):
    """Test regularizing a record where punch_out is currently None."""
    # Create employee EMP9007
    client.post("/employees", json={
        "emp_code": "EMP9007",
        "name": "Punch In Only",
        "email": "inonly@example.com",
        "department": "Support",
        "joined_on": "2026-01-01"
    })
    p_in = int(datetime(2026, 1, 13, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP9007",
        "punched_at": p_in,
        "status": "PRESENT"
    })

    # Update punch_in by 10 minutes (leaving punch_out as None)
    p_in_updated = p_in + (10 * 60 * 1000)
    r = client.patch("/attendance/EMP9007/2026-01-13", json={
        "punch_in": p_in_updated,
        "reason": "Adjusted punch in time",
        "regularized_by": "MGR001"
    })
    assert r.status_code == 200
    data = r.json()
    assert data["punch_in"] == p_in_updated
    assert data["punch_out"] is None
    assert data["work_hours"] is None

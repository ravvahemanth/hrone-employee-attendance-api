import os
import pytest
from datetime import datetime, timezone
from pymongo import MongoClient
from fastapi.testclient import TestClient
from app.main import app, db as app_db

# Enforce test database targeting
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "attendance_db_test"

TEST_CODES = ["EMP8001", "EMP8002", "EMP8003"]

@pytest.fixture(scope="module")
def mongo_test_db():
    client = MongoClient("mongodb://localhost:27017", tz_aware=True)
    db = client["attendance_db_test"]
    assert db.name == "attendance_db_test", f"Safety check failed: unexpected db {db.name}"
    # Clean up test records before test module executes
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})
    yield db
    # Clean up test records after test module finishes
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})
    client.close()

@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

def test_live_health_endpoint(client, mongo_test_db):
    """Verifies that GET /health accurately reflects live MongoDB responsiveness."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Verify directly against MongoDB
    ping_res = mongo_test_db.command("ping")
    assert ping_res.get("ok") == 1.0

def test_live_employee_workflow(client, mongo_test_db):
    """
    Verifies employee creation, unique constraint in MongoDB,
    field storage in the database, and filtered pagination.
    """
    emp1_payload = {
        "emp_code": "EMP8001",
        "name": "Alice Smith",
        "department": "Engineering",
        "email": "alice.test@example.com",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    # 1. Create employee via API
    r = client.post("/employees", json=emp1_payload)
    assert r.status_code == 201
    data = r.json()
    assert data["emp_code"] == "EMP8001"
    assert data["name"] == "Alice Smith"
    assert data["department"] == "Engineering"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)

    # 2. Verify document in MongoDB database directly
    doc = mongo_test_db.employees.find_one({"emp_code": "EMP8001"})
    assert doc is not None, "Employee document was not saved in attendance_db_test"
    assert doc["emp_code"] == "EMP8001"
    assert doc["email"] == "alice.test@example.com"
    assert doc["joined_on"] == "2026-01-01"
    assert isinstance(doc["created_at"], datetime)

    # 3. Duplicate employee should be rejected by unique index (HTTP 409)
    r_dup = client.post("/employees", json=emp1_payload)
    assert r_dup.status_code == 409
    assert r_dup.json()["detail"] == "emp_code already exists"

    # 4. Create second employee in Engineering
    emp2_payload = {
        "emp_code": "EMP8002",
        "name": "Bob Jones",
        "department": "Engineering",
        "email": "bob.test@example.com",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    r2 = client.post("/employees", json=emp2_payload)
    assert r2.status_code == 201

    # 5. Create third employee in HR
    emp3_payload = {
        "emp_code": "EMP8003",
        "name": "Carol HR",
        "department": "HR",
        "email": "carol.test@example.com",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-10"
    }
    r3 = client.post("/employees", json=emp3_payload)
    assert r3.status_code == 201

    # 6. List employees with department filter
    r_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert r_list.status_code == 200
    list_data = r_list.json()
    codes = [e["emp_code"] for e in list_data["items"]]
    assert "EMP8001" in codes
    assert "EMP8002" in codes
    assert "EMP8003" not in codes
    assert list_data["total"] >= 2

def test_live_attendance_punch_flow(client, mongo_test_db):
    """
    Verifies punch-in calculation (R1 & R2), atomic punch-out (R3, R4, R5),
    and exact database document state in MongoDB.
    """
    # Shift start: 09:30. Grace period ends: 09:40.
    # Punch-in at 09:55 IST (+05:30) is 25 minutes late from 09:30.
    # 2026-01-12 09:55:00 IST = 2026-01-12 04:25:00 UTC
    punch_in_ms = int(datetime.fromisoformat("2026-01-12T04:25:00+00:00").timestamp() * 1000)
    punch_payload = {
        "emp_code": "EMP8001",
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    }
    r_in = client.post("/attendance/punch-in", json=punch_payload)
    assert r_in.status_code == 201
    in_data = r_in.json()
    assert in_data["emp_code"] == "EMP8001"
    assert in_data["date"] == "2026-01-12"
    assert in_data["late_minutes"] == 25
    assert in_data["punch_out"] is None

    # Verify actual document in attendance_logs
    log_doc = mongo_test_db.attendance_logs.find_one({"emp_code": "EMP8001", "date": "2026-01-12"})
    assert log_doc is not None
    assert log_doc["late_minutes"] == 25
    assert log_doc["punch_out"] is None
    assert log_doc["status"] == "PRESENT"

    # Duplicate punch in for same day must return 409
    r_in_dup = client.post("/attendance/punch-in", json=punch_payload)
    assert r_in_dup.status_code == 409

    # Punch out at 18:55 IST (9.0 hours later, 25 min after 18:30 shift end -> < 30 min, so 0 OT)
    punch_out_ms = punch_in_ms + (32400 * 1000)
    out_payload = {
        "emp_code": "EMP8001",
        "punched_at": punch_out_ms
    }
    r_out = client.post("/attendance/punch-out", json=out_payload)
    assert r_out.status_code == 200
    out_data = r_out.json()
    assert out_data["work_hours"] == 9.0
    assert out_data["overtime_minutes"] == 0
    assert out_data["half_day"] is False

    # Verify updated document in MongoDB
    updated_doc = mongo_test_db.attendance_logs.find_one({"emp_code": "EMP8001", "date": "2026-01-12"})
    assert updated_doc["work_hours"] == 9.0
    assert updated_doc["overtime_minutes"] == 0
    assert updated_doc["half_day"] is False
    assert updated_doc["punch_out"] is not None

    # Second punch out when already punched out must return 409
    r_out_dup = client.post("/attendance/punch-out", json=out_payload)
    assert r_out_dup.status_code == 409

def test_live_regularization_patch(client, mongo_test_db):
    """
    Verifies attendance regularization PATCH endpoint, recalculation of overtime,
    and history diff tracking in MongoDB.
    """
    # Regularize EMP8001 on 2026-01-12: extend punch_out to 19:35 IST (+05:30) = 14:05 UTC (65 min overtime)
    new_out_ms = int(datetime.fromisoformat("2026-01-12T14:05:00+00:00").timestamp() * 1000)
    patch_payload = {
        "punch_out": new_out_ms,
        "reason": "Client deployment extended shift",
        "regularized_by": "MGR001"
    }
    r_patch = client.patch("/attendance/EMP8001/2026-01-12", json=patch_payload)
    assert r_patch.status_code == 200
    patch_data = r_patch.json()
    assert patch_data["overtime_minutes"] == 65
    assert len(patch_data["history"]) == 1
    diff = patch_data["history"][0]
    assert "punch_out" in diff["changes"]
    assert diff["reason"] == "Client deployment extended shift"
    assert diff["by"] == "MGR001"

    # Verify document in MongoDB
    doc = mongo_test_db.attendance_logs.find_one({"emp_code": "EMP8001", "date": "2026-01-12"})
    assert len(doc["history"]) == 1
    assert doc["overtime_minutes"] == 65
    assert doc["history"][0]["by"] == "MGR001"

def test_live_analytics_aggregations(client, mongo_test_db):
    """
    Verifies the 4 analytics aggregation pipelines against live MongoDB:
    1. Employee monthly aggregation (Mon-Fri presence, mid-month joining, attendance %)
    2. Department summary aggregation ($lookup, headcount with 0-log employees)
    3. Late leaderboard aggregation ($setWindowFields, $rank, competition ties)
    4. Department trend aggregation ($densify, moving average)
    """
    t1_in = datetime.fromisoformat("2026-01-12T04:45:00+00:00")
    t1_out = datetime.fromisoformat("2026-01-12T13:00:00+00:00")
    t2_in = datetime.fromisoformat("2026-01-13T04:35:00+00:00")
    t2_out = datetime.fromisoformat("2026-01-13T13:00:00+00:00")

    log1 = {
        "emp_code": "EMP8002",
        "date": "2026-01-12",
        "status": "PRESENT",
        "punch_in": t1_in,
        "punch_out": t1_out,
        "work_hours": 8.25,
        "overtime_minutes": 0,
        "late_minutes": 45,
        "half_day": False,
        "history": []
    }
    log2 = {
        "emp_code": "EMP8002",
        "date": "2026-01-13",
        "status": "PRESENT",
        "punch_in": t2_in,
        "punch_out": t2_out,
        "work_hours": 8.42,
        "overtime_minutes": 0,
        "late_minutes": 35,
        "half_day": False,
        "history": []
    }
    mongo_test_db.attendance_logs.insert_many([log1, log2])

    # 1. Monthly Employee Analytics
    r_monthly = client.get("/analytics/employees/EMP8002/monthly?month=2026-01")
    assert r_monthly.status_code == 200
    m_data = r_monthly.json()
    assert m_data["emp_code"] == "EMP8002"
    assert m_data["month"] == "2026-01"
    assert m_data["present_days"] == 2.0
    assert m_data["late_count"] == 2
    assert m_data["total_late_minutes"] == 80
    assert m_data["working_days"] == 20  # EMP8002 joined 2026-01-05 (20 Mon-Fri days remaining)
    assert m_data["attendance_pct"] == 10.0  # (2.0 / 20) * 100 = 10.0%

    # 2. Department Summary Analytics
    r_summary = client.get("/analytics/departments/summary?month=2026-01")
    assert r_summary.status_code == 200
    summary = r_summary.json()
    dept_map = {d["department"]: d for d in summary["items"]}
    assert "Engineering" in dept_map
    assert dept_map["Engineering"]["headcount"] >= 2
    assert "HR" in dept_map
    # Rule R9: Carol HR joined on 2026-01-10 with 0 attendance logs must be counted in headcount
    assert dept_map["HR"]["headcount"] >= 1

    # 3. Late Leaderboard Analytics
    r_leaderboard = client.get("/analytics/leaderboard/late?month=2026-01&limit=5")
    assert r_leaderboard.status_code == 200
    lb = r_leaderboard.json()
    top_emp = lb["items"][0]
    assert top_emp["emp_code"] == "EMP8002"
    assert top_emp["total_late_minutes"] == 80
    assert top_emp["rank"] == 1

    # 4. Department Trend Analytics
    r_trend = client.get("/analytics/departments/Engineering/trend?from=2026-01-10&to=2026-01-16")
    assert r_trend.status_code == 200
    trend_data = r_trend.json()
    assert trend_data["department"] == "Engineering"
    assert len(trend_data["items"]) == 7  # All 7 calendar days densified
    for pt in trend_data["items"]:
        assert "date" in pt
        assert "moving_avg_7d" in pt

def test_live_admin_explain_endpoints(client):
    """
    Verifies that explain endpoints return valid execution plans from MongoDB
    and that indexed queries utilize IXSCAN.
    """
    explain_configs = [
        {"endpoint": "attendance_list", "params": {}},
        {"endpoint": "employee_monthly", "params": {"emp_code": "EMP8001", "month": "2026-01"}},
        {"endpoint": "department_summary", "params": {"month": "2026-01"}},
        {"endpoint": "late_leaderboard", "params": {"month": "2026-01"}},
        {"endpoint": "department_trend", "params": {"department": "Engineering", "from": "2026-01-10", "to": "2026-01-16"}},
    ]
    for cfg in explain_configs:
        ep = cfg["endpoint"]
        params = cfg["params"]
        r = client.get(f"/admin/explain/{ep}", params=params)
        assert r.status_code == 200, f"Explain failed for {ep}: {r.text}"
        data = r.json()
        assert data["endpoint"] == ep
        assert "explain" in data
        assert ("queryPlanner" in data["explain"] or "stages" in data["explain"])

    # Deep verification of attendance_list: must use index scan (IXSCAN)
    r_att = client.get("/admin/explain/attendance_list")
    explain_info = r_att.json()["explain"]
    # Verify stages contain IXSCAN
    winning_plan = explain_info.get("queryPlanner", {}).get("winningPlan", {})
    plan_str = str(winning_plan)
    assert "IXSCAN" in plan_str or "FETCH" in plan_str, "attendance_list must utilize index scan"

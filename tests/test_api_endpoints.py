"""
API validation, error handling, and contract compliance tests.
Uses FastAPI TestClient to test status codes, schemas, and error responses.
"""
from unittest.mock import MagicMock, patch
import pytest
import pymongo.collection
import pymongo.database
from fastapi.testclient import TestClient
from app.main import app, db


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health_503_when_mongo_down(client):
    """GET /health must return 503 when MongoDB ping fails."""
    with patch.object(pymongo.database.Database, "command", side_effect=Exception("Connection refused")):
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}


def test_health_200_when_mongo_up(client):
    """GET /health must return 200 when MongoDB ping succeeds."""
    with patch.object(pymongo.database.Database, "command", return_value={"ok": 1.0}):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_employee_create_validations(client):
    """POST /employees validation tests."""
    # 1. Invalid emp_code format (must be ^EMP\d{4,6}$)
    res = client.post("/employees", json={
        "emp_code": "INVALID_CODE",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # 2. shift_start must differ from shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0100",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # 3. Invalid email format
    res = client.post("/employees", json={
        "emp_code": "EMP0100",
        "name": "Test User",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422


def test_punch_in_validations(client):
    """POST /attendance/punch-in validation tests."""
    # 1. Reject timestamp as string
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": "1783312500000"
    })
    assert res.status_code == 422

    # 2. Reject timestamp as float
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500000.5
    })
    assert res.status_code == 422

    # 3. Reject timestamp in seconds scale (< 100000000000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500
    })
    assert res.status_code == 422

    # 4. Reject invalid status (only PRESENT, WFH, ON_DUTY allowed for punch-in)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "status": "ABSENT"
    })
    assert res.status_code == 422

    # 5. Unknown employee returns 404
    with patch.object(pymongo.collection.Collection, "find_one", return_value=None):
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9999",
            "punched_at": 1783312500000,
            "status": "PRESENT"
        })
        assert res.status_code == 404
        assert res.json() == {"detail": "Employee not found"}


def test_attendance_list_validations(client):
    """GET /attendance query parameter validations."""
    # date_from > date_to -> 422
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res.status_code == 422
    assert "date_from must be <= date_to" in res.json()["detail"]

    # page_size > 100 -> 422
    res = client.get("/attendance?page_size=101")
    assert res.status_code == 422

    # page < 1 -> 422
    res = client.get("/attendance?page=0")
    assert res.status_code == 422


def test_regularize_validations(client):
    """PATCH /attendance/{emp_code}/{date} validations."""
    def fake_find_one(self, filter, *args, **kwargs):
        if "emp_code" in filter and "date" not in filter:
            return {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        if "date" in filter:
            return {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "history": []}
        return None

    # 1. Changes nothing -> 422
    with patch.object(pymongo.collection.Collection, "find_one", fake_find_one):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "Test update reason",
            "regularized_by": "admin"
        })
        assert res.status_code == 422
        assert "No changes supplied" in res.json()["detail"]

    # 2. Supplying punch times when setting status ABSENT -> 422
    with patch.object(pymongo.collection.Collection, "find_one", fake_find_one):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": 1783312500000,
            "reason": "Absent recorded",
            "regularized_by": "admin"
        })
        assert res.status_code == 422
        assert "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE" in res.json()["detail"]


def test_department_trend_validations(client):
    """GET /analytics/departments/{department}/trend validations."""
    # 1. Unknown department -> 404
    with patch.object(pymongo.collection.Collection, "count_documents", return_value=0):
        res = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-10")
        assert res.status_code == 404
        assert res.json() == {"detail": "Department not found"}

    # 2. to < from -> 422
    with patch.object(pymongo.collection.Collection, "count_documents", return_value=5):
        res = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
        assert res.status_code == 422
        assert "to date must be >= from date" in res.json()["detail"]

    # 3. Range exceeds 92 days -> 422
    with patch.object(pymongo.collection.Collection, "count_documents", return_value=5):
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-15")
        assert res.status_code == 422
        assert "Date range cannot exceed 92 days" in res.json()["detail"]


def test_explain_validations(client):
    """GET /admin/explain/{endpoint} validations."""
    # 1. Unsupported endpoint name -> 422
    res = client.get("/admin/explain/invalid_endpoint_name")
    assert res.status_code == 422

    # 2. Missing required params for employee_monthly -> 422
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    # 3. Missing required params for department_summary -> 422
    res = client.get("/admin/explain/department_summary")
    assert res.status_code == 422

    # 4. Missing required params for department_trend -> 422
    res = client.get("/admin/explain/department_trend")
    assert res.status_code == 422

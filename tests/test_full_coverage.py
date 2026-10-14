"""
Dedicated full-coverage test suite to achieve 100% statement and branch coverage
for all defensive, fallback, and boundary logic in app/main.py.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
import pymongo.collection
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import (
    app,
    init_db_indexes,
    serialize_employee,
    explain_endpoint,
)

COVERAGE_TEST_CODES = ["EMP7001", "EMP7002", "EMP7003"]

@pytest.fixture(scope="module")
def cov_mongo_db():
    client = MongoClient("mongodb://localhost:27017", tz_aware=True)
    db = client["attendance_db_test"]
    assert db.name == "attendance_db_test"
    db.employees.delete_many({"emp_code": {"$in": COVERAGE_TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": COVERAGE_TEST_CODES}})
    yield db
    db.employees.delete_many({"emp_code": {"$in": COVERAGE_TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": COVERAGE_TEST_CODES}})
    client.close()

@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

def test_init_db_indexes_exception_handling():
    """Covers lines 61-64: exception handling when MongoDB fails during startup index creation."""
    with patch.object(pymongo.collection.Collection, "create_index", side_effect=Exception("Connection refused")):
        # Must not crash, gracefully catches exception
        init_db_indexes()

def test_serialize_employee_unhandled_date_format():
    """Covers line 242: fallback when $date value is neither int, float, nor ISO str."""
    doc = {
        "emp_code": "EMP7001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "IT",
        "joined_on": "2026-01-01",
        "created_at": {"$date": None}
    }
    serialized = serialize_employee(doc)
    assert serialized["created_at"] == 0

def test_list_employees_without_department_filter(client, cov_mongo_db):
    """Covers line 417->420 branch: list_employees without department filter."""
    res = client.get("/employees?page=1&page_size=5")
    assert res.status_code == 200
    assert "items" in res.json()

def test_punch_out_with_naive_datetime(client, cov_mongo_db):
    """Covers line 501: punch_out when punch_in record has naive datetime (tzinfo is None)."""
    client.post("/employees", json={
        "emp_code": "EMP7001",
        "name": "Naive Time User",
        "email": "naive@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    naive_in = datetime(2026, 1, 15, 4, 0, 0)  # tzinfo is None
    mock_record = {
        "_id": "mock_id_123",
        "emp_code": "EMP7001",
        "date": "2026-01-15",
        "status": "PRESENT",
        "punch_in": naive_in,
        "punch_out": None,
        "history": []
    }
    mock_updated = {
        **mock_record,
        "punch_out": datetime(2026, 1, 15, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "overtime_minutes": 0,
        "half_day": False
    }

    orig_find_one = pymongo.collection.Collection.find_one
    orig_find_update = pymongo.collection.Collection.find_one_and_update

    def mock_find_one(self, filter, *args, **kwargs):
        if self.name == "attendance_logs":
            return mock_record
        return orig_find_one(self, filter, *args, **kwargs)

    def mock_find_one_and_update(self, filter, update, *args, **kwargs):
        if self.name == "attendance_logs":
            return mock_updated
        return orig_find_update(self, filter, update, *args, **kwargs)

    with patch.object(pymongo.collection.Collection, "find_one", mock_find_one), \
         patch.object(pymongo.collection.Collection, "find_one_and_update", mock_find_one_and_update):
        out_ms = int(datetime(2026, 1, 15, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP7001",
            "punched_at": out_ms
        })
        assert res.status_code == 200
        assert res.json()["work_hours"] == 9.0

def test_regularize_presence_status_without_punch_in(client, cov_mongo_db):
    """
    Covers line 623: regularize attendance to presence status (PRESENT/WFH)
    when existing record has status ABSENT (punch_in is None) and no punch_in is supplied.
    """
    client.post("/employees", json={
        "emp_code": "EMP7002",
        "name": "Absent User",
        "email": "absent@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    cov_mongo_db.attendance_logs.insert_one({
        "emp_code": "EMP7002",
        "date": "2026-01-16",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })
    res = client.patch("/attendance/EMP7002/2026-01-16", json={
        "status": "PRESENT",
        "reason": "Trying to mark present without punch_in",
        "regularized_by": "MGR001"
    })
    assert res.status_code == 422
    assert "presence status requires punch_in" in res.json()["detail"].lower()

def test_regularize_with_naive_punch_in_and_out(client, cov_mongo_db):
    """
    Covers lines 626 & 643: regularization when existing record has naive datetimes
    for punch_in and punch_out.
    """
    client.post("/employees", json={
        "emp_code": "EMP7003",
        "name": "Naive Regularize User",
        "email": "naive.reg@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    naive_in = datetime(2026, 1, 16, 4, 0, 0)    # tzinfo is None
    naive_out = datetime(2026, 1, 16, 13, 0, 0)  # tzinfo is None
    mock_record = {
        "_id": "mock_id_456",
        "emp_code": "EMP7003",
        "date": "2026-01-16",
        "status": "PRESENT",
        "punch_in": naive_in,
        "punch_out": naive_out,
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_updated = {
        **mock_record,
        "status": "WFH",
        "punch_in": naive_in.replace(tzinfo=timezone.utc),
        "punch_out": naive_out.replace(tzinfo=timezone.utc),
    }

    orig_find_one = pymongo.collection.Collection.find_one
    orig_find_update = pymongo.collection.Collection.find_one_and_update

    def mock_find_one(self, filter, *args, **kwargs):
        if self.name == "attendance_logs":
            return mock_record
        return orig_find_one(self, filter, *args, **kwargs)

    def mock_find_one_and_update(self, filter, update, *args, **kwargs):
        if self.name == "attendance_logs":
            return mock_updated
        return orig_find_update(self, filter, update, *args, **kwargs)

    with patch.object(pymongo.collection.Collection, "find_one", mock_find_one), \
         patch.object(pymongo.collection.Collection, "find_one_and_update", mock_find_one_and_update):
        res = client.patch("/attendance/EMP7003/2026-01-16", json={
            "status": "WFH",
            "reason": "Change status to WFH keeping naive times",
            "regularized_by": "MGR001"
        })
        assert res.status_code == 200
        assert res.json()["status"] == "WFH"

def test_admin_explain_attendance_list_single_date_filters(client):
    """Covers line 1350->1352 and 1352->1355 branches: explain with single date boundaries."""
    # 1. only date_from
    r1 = client.get("/admin/explain/attendance_list?date_from=2026-01-01")
    assert r1.status_code == 200
    assert "explain" in r1.json()

    # 2. only date_to
    r2 = client.get("/admin/explain/attendance_list?date_to=2026-01-31")
    assert r2.status_code == 200
    assert "explain" in r2.json()

def test_explain_endpoint_unsupported_fallback():
    """Covers line 1441: explain_endpoint fallback when unsupported endpoint passed."""
    mock_ep = MagicMock()
    mock_ep.value = "unsupported_endpoint"
    with pytest.raises(HTTPException) as exc_info:
        explain_endpoint(endpoint=mock_ep)
    assert exc_info.value.status_code == 422
    assert "unsupported endpoint" in exc_info.value.detail.lower()

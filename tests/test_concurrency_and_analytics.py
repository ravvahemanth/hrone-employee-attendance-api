"""
Tests for concurrency race handling (simultaneous punch-in, punch-out, and regularization)
and leaderboard standard competition ranking logic.
"""
from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from pymongo.errors import DuplicateKeyError
import pymongo.collection
from fastapi.testclient import TestClient
from app.main import app, db, build_leaderboard_pipeline, build_department_summary_pipeline, build_department_trend_pipeline


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_concurrent_employee_creation_duplicate_key(client):
    """
    When two identical employee creation requests race,
    the second insert raises DuplicateKeyError from MongoDB unique index,
    which must be caught and return 409.
    """
    with patch.object(pymongo.collection.Collection, "insert_one", side_effect=DuplicateKeyError("E11000 duplicate key error")):
        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05"
        })
        assert res.status_code == 409
        assert res.json() == {"detail": "emp_code already exists"}


def test_concurrent_punch_in_duplicate_key(client):
    """
    When two identical punch-in requests arrive simultaneously,
    the database-enforced unique index on (emp_code, date) prevents duplicates.
    The loser receives 409.
    """
    fake_emp = {
        "emp_code": "EMP0001",
        "shift_start": "09:30",
        "shift_end": "18:30"
    }
    with patch.object(pymongo.collection.Collection, "find_one", return_value=fake_emp), \
         patch.object(pymongo.collection.Collection, "insert_one", side_effect=DuplicateKeyError("E11000 duplicate key error")):
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1783312500000,
            "status": "PRESENT"
        })
        assert res.status_code == 409
        assert res.json() == {"detail": "already punched in for this date"}


def test_concurrent_punch_out_atomic_update(client):
    """
    When two punch-out requests race, find_one_and_update matches {"punch_out": None}.
    Only the first caller succeeds; the second caller gets None back and returns 409.
    """
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "fake_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": None
    }
    def mock_find_one(self, filter, *args, **kwargs):
        if "punch_in" in filter:
            return fake_record
        return fake_emp

    with patch.object(pymongo.collection.Collection, "find_one", mock_find_one), \
         patch.object(pymongo.collection.Collection, "find_one_and_update", return_value=None):
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": 1783350000000
        })
        assert res.status_code == 409
        assert res.json() == {"detail": "Record already punched out"}


def test_concurrent_regularization_conflict(client):
    """
    When two regularizations race on the same record, optimistic concurrency control
    on history size ensures the second update returns None and results in 409.
    """
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "fake_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    def mock_find_one(self, filter, *args, **kwargs):
        if "date" in filter:
            return fake_record
        return fake_emp

    with patch.object(pymongo.collection.Collection, "find_one", mock_find_one), \
         patch.object(pymongo.collection.Collection, "find_one_and_update", return_value=None):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "WFH",
            "reason": "Corrected to WFH",
            "regularized_by": "manager"
        })
        assert res.status_code == 409
        assert res.json() == {"detail": "Concurrent modification conflict"}


def test_leaderboard_pipeline_structure():
    """Verify that the leaderboard aggregation pipeline uses $setWindowFields and $rank."""
    pipe = build_leaderboard_pipeline("2026-07", limit=10, department="Engineering")
    operators = [list(stage.keys())[0] for stage in pipe]
    assert "$match" in operators
    assert "$group" in operators
    assert "$setWindowFields" in operators
    assert "$sort" in operators

    # Check window ranking stage
    window_stage = [s for s in pipe if "$setWindowFields" in s][0]
    output = window_stage["$setWindowFields"]["output"]
    assert "rank" in output
    assert "$rank" in output["rank"]


def test_department_summary_pipeline_structure():
    """Verify department summary pipeline includes cross-collection lookup and date filtering."""
    pipe = build_department_summary_pipeline("2026-07", "Engineering")
    operators = [list(stage.keys())[0] for stage in pipe]
    assert "$match" in operators
    assert "$lookup" in operators
    assert "$group" in operators
    assert "$project" in operators


def test_department_trend_pipeline_structure():
    """Verify department trend pipeline includes $densify, $setWindowFields for 7-day moving avg."""
    pipe = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-15")
    operators = [list(stage.keys())[0] for stage in pipe]
    assert "$match" in operators
    assert "$densify" in operators
    assert "$setWindowFields" in operators
    # Check moving average window definition
    window_stage = [s for s in pipe if "$setWindowFields" in s][0]
    output = window_stage["$setWindowFields"]["output"]
    assert "moving_avg_7d" in output
    assert output["moving_avg_7d"]["window"]["documents"] == [-6, "current"]

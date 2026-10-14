"""
Employee Attendance & Analytics API

Stack: Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+
Entry point: app/main.py
Run command: uvicorn app.main:app --port 8000
"""

import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, StrictInt, model_validator, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load .env without overriding existing environment variables
# (actual environment variables take priority)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# PyMongo client configured with UTC timezone awareness
client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
)
db = client[MONGO_DB]

# Timezone constant for IST (UTC+05:30) per rule R1
IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes() -> None:
    """
    Creates all required MongoDB indexes idempotently at application startup.
    This ensures uniqueness constraints and guarantees IXSCAN for evaluated queries.
    """
    try:
        # employees collection indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_emp_dept_code")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")
        db.employees.create_index([("joined_on", 1)], name="idx_emp_joined_on")

        # attendance_logs collection indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_att_emp_date_desc")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in_desc")
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_att_date_late")
        db.attendance_logs.create_index([("date", 1), ("emp_code", 1)], name="idx_att_date_emp")
    except Exception:
        # Do not block application startup if MongoDB is temporarily unavailable;
        # /health will correctly return 503 until MongoDB is reachable.
        pass


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Lifecycle manager for application startup and shutdown."""
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Reusable Mathematical and Date Helpers (Rules R1 - R8)
# --------------------------------------------------------------------------- #

def round_half_up(value: Optional[float], decimals: int = 2) -> Optional[float]:
    """
    Rounds a number to the specified number of decimal places using Decimal
    ROUND_HALF_UP (arithmetic rounding: 0.5 rounds away from zero, not banker's rounding).
    """
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """
    Converts epoch milliseconds to a UTC datetime, truncated to whole seconds (R1).
    """
    if ms is None:
        return None
    # Whole seconds truncation: drop remainder milliseconds
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a datetime to integer epoch milliseconds since 1970-01-01T00:00:00Z.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """
    Checks if shift is overnight (shift_end <= shift_start) per R1.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Derives attendance date from IST calendar date of punch_in (R1).
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than
    shift_end belongs to the shift that started on the previous calendar day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (eh, em, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    Calculates late minutes according to R2:
    - 10-minute grace period.
    - Late only if strictly more than 10 minutes after scheduled shift_start.
    - When late, late_minutes = whole minutes (floor) since shift_start.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    ad = date.fromisoformat(attendance_date)
    sched_start = datetime(ad.year, ad.month, ad.day, sh, sm, 0, tzinfo=IST)
    diff_seconds = (punch_ist - sched_start).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    Calculates elapsed hours between punch_out and punch_in (R4).
    Rounded to 2 decimal places using Decimal ROUND_HALF_UP.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    d = Decimal(str(int(diff_sec))) / Decimal("3600")
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_dt: datetime, shift_end: str, attendance_date: str, shift_start: str
) -> int:
    """
    Calculates overtime minutes according to R3:
    - Whole minutes between scheduled shift_end and punch_out.
    - Counted only when difference is at least 30 minutes (>= 30).
    - Otherwise 0.
    - Handles overnight shifts where scheduled shift_end is on the following day.
    """
    punch_ist = punch_out_dt.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    ad = date.fromisoformat(attendance_date)
    if is_overnight_shift(shift_start, shift_end):
        end_date = ad + timedelta(days=1)
    else:
        end_date = ad
    sched_end = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    diff_seconds = (punch_ist - sched_end).total_seconds()
    diff_minutes = int(diff_seconds // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    Calculates half-day status (R5):
    After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    Counts Monday-Friday working days in the specified month (R7).
    For employees who joined mid-month, counts only working days from joined_on onward.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)
    joined_date = date.fromisoformat(joined_on_str.strip())
    if joined_date > last_day:
        return 0
    start_date = max(first_day, joined_date)
    cur = start_date
    working_days = 0
    while cur <= last_day:
        if cur.weekday() < 5:  # Mon-Fri
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Document Serializers (Translating MongoDB BSON to OpenAPI Schemas)
# --------------------------------------------------------------------------- #

def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Converts MongoDB employee document to OpenAPI Employee schema."""
    created_at_val = doc.get("created_at")
    if isinstance(created_at_val, datetime):
        created_at_ms = dt_to_epoch_ms(created_at_val)
    elif isinstance(created_at_val, (int, float)):
        created_at_ms = int(created_at_val)
    elif isinstance(created_at_val, dict) and "$date" in created_at_val:
        d_val = created_at_val["$date"]
        if isinstance(d_val, (int, float)):
            created_at_ms = int(d_val)
        elif isinstance(d_val, str):
            created_at_ms = dt_to_epoch_ms(datetime.fromisoformat(d_val.replace("Z", "+00:00")))
        else:
            created_at_ms = 0
    else:
        created_at_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes an audit history entry for regularizations."""
    at_val = entry.get("at")
    at_ms = dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

    changes_out: Dict[str, Any] = {}
    for field, diff in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            from_v = diff.get("from")
            to_v = diff.get("to")
            changes_out[field] = {
                "from": dt_to_epoch_ms(from_v) if isinstance(from_v, datetime) else from_v,
                "to": dt_to_epoch_ms(to_v) if isinstance(to_v, datetime) else to_v,
            }
        else:
            changes_out[field] = {
                "from": diff.get("from"),
                "to": diff.get("to"),
            }

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_out,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Converts MongoDB attendance_logs document to OpenAPI AttendanceRecord schema."""
    history_entries = [serialize_history_entry(h) for h in doc.get("history", [])]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching OpenAPI contract
# --------------------------------------------------------------------------- #

class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatusEnum = Field(default=PresenceStatusEnum.PRESENT)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoint
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """
    Readiness probe: returns HTTP 200 only if MongoDB responds successfully;
    otherwise returns HTTP 503.
    """
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Management Endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Unique emp_code is enforced at database level.
    Concurrent duplicates yield exactly one 201 and the rest 409.
    """
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists employees sorted by emp_code ascending.
    Total counts the filtered set, not the whole collection.
    """
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Punch-in and Punch-out Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Records a punch-in for an employee.
    Derives date using R1 and late_minutes using R2.
    Race-safe: enforced by unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    doc["_id"] = res.inserted_id
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Records punch-out by closing the employee's most recent record whose punch_in
    is at or before punched_at.
    Uses atomic find_one_and_update to guarantee race safety.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_minutes = compute_overtime(punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    # Atomic update matching punch_out: None to ensure simultaneous requests cannot both succeed
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": ot_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusEnum] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists attendance records sorted by date descending, then emp_code ascending.
    Indexed and paginated in MongoDB to remain fast on large collections.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status.value
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Manual Correction (Regularization) Endpoint
# --------------------------------------------------------------------------- #

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Regularizes an existing attendance record for an employee on a specified date.
    Recalculates all derived fields and appends an audit history entry with only
    the fields that actually changed.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # A request that supplies no fields to change is rejected with 422
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="No changes supplied")

    target_status = body.status.value if body.status is not None else record["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times;
    # supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status (PRESENT, WFH, ON_DUTY) requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if not new_punch_in:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date (R1)
        derived_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in derives attendance date {derived_date}, not matching record date {date}",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

    # Detect fields that actually changed
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes_mongo: Dict[str, Any] = {}

    if old_status != target_status:
        changes_mongo["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != new_punch_in:
        changes_mongo["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes_mongo["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if old_work_hours != new_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes_mongo["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(status_code=422, detail="Request produces no actual changes")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    audit_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    # Optimistic concurrency: ensure history array length matches to prevent lost audit entries
    current_history = record.get("history", [])
    expected_history_len = len(current_history)

    update_filter = {
        "_id": record["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history_len]},
    }

    update_ops = {
        "$set": {
            "status": target_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": audit_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        update_ops,
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    Reads stored derived values per specification.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    last_day_str = f"{month}-{num_days:02d}"

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": last_day_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$dayOfWeek", 2]},
                                    {"$lte": ["$dayOfWeek", 6]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """Builds MongoDB cross-collection aggregation pipeline for department summary."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    last_day_str = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", last_day_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$let": {
                                                    "vars": {
                                                        "dow": {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {"dateString": "$$log.date"}
                                                            }
                                                        }
                                                    },
                                                    "in": {
                                                        "$and": [
                                                            {"$gte": ["$$dow", 2]},
                                                            {"$lte": ["$$dow", 6]},
                                                        ]
                                                    },
                                                }
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "plog",
                            "in": {"$cond": [{"$ifNull": ["$$plog.half_day", False]}, 0.5, 1.0]},
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$isNumber": "$$log.work_hours"},
                                        ]
                                    },
                                }
                            },
                            "as": "wh_log",
                            "in": "$$wh_log.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)."),
):
    """
    Department summary for a month via MongoDB aggregation across both collections.
    Headcount includes qualifying employees without logs per R9.
    """
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    items: List[Dict[str, Any]] = []
    for doc in cursor:
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": doc["present_days"],
            "avg_work_hours": round_half_up(doc["avg_work_hours"], 2),
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })
    return {"month": month, "items": items}


def build_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Builds aggregation pipeline for late leaderboard using standard competition ranking."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    last_day_str = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    Ranks employees by total late minutes in the month using standard competition ranking.
    Ties share a rank (1, 2, 2, 4) and all tied employees <= limit are included.
    """
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    """
    Builds MongoDB aggregation pipeline for daily attendance trend:
    - Filters by department and date range.
    - Densifies to produce exactly one row per calendar day.
    - Computes attendance_rate and 7-day moving average inside MongoDB.
    """
    from_dt = datetime.fromisoformat(from_date + "T00:00:00+00:00")
    to_dt_end = datetime.fromisoformat(to_date + "T00:00:00+00:00") + timedelta(days=1)

    return [
        # Stage 1: Filter attendance logs in date range (uses date index)
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        # Stage 2: Join employees to match department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # Stage 3: Group by date to aggregate present and late records
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        # Union with a single document from employees to guarantee bounds for densification
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        # Re-group by date to consolidate any duplicated bound date
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        # Convert date string to Date object for densify stage
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Stage: $densify fills missing calendar dates in range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_end],
                },
            }
        },
        # Project date string, day of week, and working day flag
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            }
        },
        # Lookup headcount of employees in department with joined_on <= date (R9)
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Calculate attendance_rate
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        # Calculate 7-calendar-day moving average using window function
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department with gap-filling ($densify) and
    7-day moving average in MongoDB.
    """
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date must be >= from date")

    day_span = (d_to - d_from).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)

    items: List[Dict[str, Any]] = []
    for doc in cursor:
        items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": doc["present_count"],
            "late_count": doc["late_count"],
            "attendance_rate": round_half_up(doc["attendance_rate"], 4),
            "moving_avg_7d": round_half_up(doc["moving_avg_7d"], 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusEnum] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Executes MongoDB explain with executionStats verbosity on the main query or
    aggregation pipeline of the named endpoint.
    """
    ep_name = endpoint.value

    if ep_name == "attendance_list":
        coll_name = "attendance_logs"
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status.value
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        command = {
            "find": coll_name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": ep_name, "collection": coll_name, "explain": explain_res}

    elif ep_name == "employee_monthly":
        coll_name = "attendance_logs"
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        last_day_str = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": last_day_str}}},
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": ["$dayOfWeek", 2]},
                                        {"$lte": ["$dayOfWeek", 6]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        command = {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": ep_name, "collection": coll_name, "explain": explain_res}

    elif ep_name == "department_summary":
        coll_name = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        command = {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": ep_name, "collection": coll_name, "explain": explain_res}

    elif ep_name == "late_leaderboard":
        coll_name = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_leaderboard_pipeline(month, limit or 10, department)
        command = {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": ep_name, "collection": coll_name, "explain": explain_res}

    elif ep_name == "department_trend":
        coll_name = "attendance_logs"
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to parameters are required")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        command = {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": ep_name, "collection": coll_name, "explain": explain_res}

    raise HTTPException(status_code=422, detail="Unsupported endpoint")

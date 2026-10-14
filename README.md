# Employee Attendance & Analytics API

A robust FastAPI backend service backed by MongoDB 6.0+ for managing employee records, real-time biometric punch-ins/punch-outs, manual attendance corrections with complete audit trails, and high-performance MongoDB aggregation pipelines for attendance analytics.

## Prerequisites

- **Python**: 3.11 or newer (Python 3.12 supported)
- **MongoDB**: 6.0 or newer (tested with MongoDB 6.0/7.0, local instance or MongoDB Atlas)

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI string | `mongodb://localhost:27017` |
| `MONGO_DB` | Target database name | `attendance_db` |

*Note: Real environment variables take priority over any local `.env` file.*

## Installation & Setup

1. Clone repository and navigate to root directory:
   ```bash
   cd candidate_kit
   ```

2. Create and activate a Python virtual environment:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Optional seed data load (if running against a fresh MongoDB instance):
   ```bash
   python sample_seed.py
   ```

## Running the Application

Start the FastAPI application with Uvicorn:
```bash
uvicorn app.main:app --port 8000
```

- API Base URL: `http://localhost:8000`
- Interactive OpenAPI Docs: `http://localhost:8000/docs`
- Health Readiness Probe: `http://localhost:8000/health`

## Running Automated Tests

Run the complete test suite using pytest:
```bash
python -m pytest tests/ -v
```

## Implemented Features

1. **System & Health**:
   - `GET /health` readiness probe returning HTTP 200 on active MongoDB ping and HTTP 503 if unreachable.
   - Idempotent index creation during application startup.

2. **Employee Management**:
   - `POST /employees`: Create employee with regex validations and unique `emp_code` race-safety (409 on duplicate).
   - `GET /employees`: Filtered by department, sorted by `emp_code` ascending, with accurate paginated totals.

3. **Attendance Management**:
   - `POST /attendance/punch-in`: Derives IST calendar date (R1), calculates late arrival (R2) with 10-minute grace period, race-safe against concurrent punch-ins via compound unique index `(emp_code, date)`.
   - `POST /attendance/punch-out`: Finds most recent matching punch-in across overnight shifts, atomic conditional update preventing duplicate punch-outs, calculates work hours (R4) with Decimal half-up rounding, overtime (R3) with 30-min minimum, and half-day status (R5).
   - `GET /attendance`: Server-side filtered, indexed, and paginated by `date desc, emp_code asc`.
   - `PATCH /attendance/{emp_code}/{date}`: Manual attendance regularizations with complete recalculation and audit history of changed fields (`history[].changes`).

4. **Analytics Endpoints (MongoDB Aggregations)**:
   - `GET /analytics/employees/{emp_code}/monthly`: Employee monthly statistics using MongoDB aggregation, R7 weekday counting, and presence tracking.
   - `GET /analytics/departments/summary`: Cross-collection `$lookup` aggregation accurately counting headcount for all eligible employees (R9) including employees without attendance logs.
   - `GET /analytics/leaderboard/late`: Late-comers leaderboard using standard competition ranking (`$setWindowFields` with `$rank`), handling ties across cutoff limits.
   - `GET /analytics/departments/{department}/trend`: Gap-filled daily attendance trend via `$densify`, weekday filtering, and 7-day moving average calculation.

5. **Admin & Performance**:
   - `GET /admin/explain/{endpoint}`: MongoDB execution plan inspection with `executionStats` verbosity.

## Verification & Testing Notes

- All 32 automated tests (unit tests, API contract validations, concurrency race simulations, and live MongoDB integration tests) pass with 100% success.
- Indexes are explicitly created at startup to ensure `IXSCAN` and prevent unintended `COLLSCAN`.

# DECISIONS.md

1. **Indexes.** We created unique indexes `employees(emp_code)` and `attendance_logs(emp_code, date)` to guarantee identity and record uniqueness. For queries, we added `employees(department, emp_code)` and `employees(joined_on)`, alongside `attendance_logs(date: -1, emp_code: 1)` for listing pagination, `attendance_logs(emp_code, punch_in: -1)` for punch-out resolution, and `attendance_logs(date, late_minutes)` for leaderboard scans. We considered and rejected indexing `status` alone, as low-cardinality flags yield poor selectivity and add unnecessary write overhead.

2. **Punch-in race.** When two identical punch-in requests arrive simultaneously, both derive the same attendance date. Both execute `insert_one()`. MongoDB's unique compound index on `(emp_code, date)` atomically admits exactly one write (returning HTTP 201); the competing operation triggers a unique key violation, caught as a `DuplicateKeyError` and converted to HTTP 409.

3. **Ties.** The leaderboard pipeline applies standard competition ranking via `$setWindowFields` with `$rank` (1, 2, 2, 4). The filter `rank <= limit` runs *after* ranking, so tied employees at the cutoff share the same rank and are all returned, ordered by `emp_code` ascending.

4. **Headcount.** The department summary pipeline originates from `employees` (filtering `joined_on <= period_end`) and groups on department with `{"$sum": 1}`, ensuring all eligible personnel count towards headcount even with zero attendance records.

5. **100x scale change.** At 100x scale (~10M records), sharding `attendance_logs` by `{department: 1, date: 1}` and pre-computing daily attendance rollups would keep analytics sub-second without scanning raw event streams.

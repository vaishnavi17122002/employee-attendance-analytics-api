# DECISIONS.md

1. **Indexes.**
We created:
- `uq_employee_code` (`emp_code`, unique) and `uq_attendance_employee_date` (`emp_code, date`, unique) to enforce natural key uniqueness and prevent race conditions.
- `ix_attendance_date_code` (`date DESC, emp_code ASC`) to support fast pagination for `/attendance`.
- `ix_attendance_date_late` (`date ASC, late_minutes DESC`) to enable efficient index scans for the late leaderboard.
- `ix_employee_department_joined` (`department ASC, joined_on ASC`) for department trend and summary lookups.
We rejected separate indexes on naive timestamp fields like `punch_in` or nested `history` fields as they are covered by `(emp_code, date)`.

2. **Punch-in race.**
Both concurrent requests attempt an atomic `insert_one`. MongoDB's unique index on `(emp_code, date)` ensures that exactly one write commits (HTTP 201). The second write fails with `DuplicateKeyError`, caught by our handler to immediately return HTTP 409 Conflict without dirty reads.

3. **Ties.**
Using `$setWindowFields` with `$rank`, tied employees share the identical rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4). When ties occur at the cutoff (`rank <= limit`), all tied candidates are retained and ordered deterministically by `total_late_minutes DESC, emp_code ASC`.

4. **Headcount.**
The pipeline originates from `employees` (filtering `joined_on <= period_end`) and performs a left-outer `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: True`. Employees without any attendance documents are thus preserved and accurately included in `$addToSet: "$emp_code"`.

5. **One thing you would change.**
At 100x scale (~10M+ documents), I would maintain pre-aggregated daily/monthly summary collections updated asynchronously or via change streams, avoiding expensive real-time aggregations over raw log records.

"""
Employee Attendance & Analytics API.
Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI, MONGO_DB (environment variables override .env).
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId, json_util
from bson.json_util import CANONICAL_JSON_OPTIONS
import json

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- Validation --------------------------------
def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def validate_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(month + "-01")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1)
    return start, end


def epoch_ms_to_dt(value: Any, field: str = "punched_at") -> datetime:
    # bool is an int subclass; floats/strings and second-based timestamps are intentionally rejected.
    if type(value) is not int or value < EPOCH_MIN or value > EPOCH_MAX:
        raise HTTPException(422, f"{field} must be an integer epoch-millisecond timestamp in the supported range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = as_utc(value).replace(microsecond=0)
    return int(value.timestamp() * 1000)


def local_dt(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def shift_datetime(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = local_dt(punch_in)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def late_minutes(punch_in: datetime, shift_start: str, shift_end: str, record_day: date) -> int:
    local = local_dt(punch_in)
    start_day = record_day
    start = shift_datetime(start_day, shift_start)
    elapsed = (local - start).total_seconds()
    return int(elapsed // 60) if elapsed > 600 else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, record_day: date) -> int:
    end_day = record_day + timedelta(days=1) if shift_end <= shift_start else record_day
    end = shift_datetime(end_day, shift_end)
    elapsed = (local_dt(punch_out) - end).total_seconds()
    whole = int(elapsed // 60)
    return whole if whole >= 30 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    rounded = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def clean_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_ms(doc["created_at"])
    return doc


def clean_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)
    for field in ("punch_in", "punch_out"):
        if isinstance(doc.get(field), datetime):
            doc[field] = to_ms(doc[field])
        else:
            doc.setdefault(field, None)
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            change = entry.get("changes", {}).get(field)
            if change:
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = to_ms(change[side])
    return doc


def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_status_late")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_late")


@app.on_event("startup")
def startup():
    try:
        ensure_indexes()
    except PyMongoError:
        # Health endpoint will report the database as unavailable; startup remains diagnosable.
        pass


# --------------------------------- Models ----------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def valid_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_times(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


# ------------------------------- App helpers -------------------------------
def attendance_pipeline(query: dict, page: int, page_size: int):
    return [
        {"$match": query},
        {"$sort": {"date": -1, "emp_code": 1}},
        {"$facet": {
            "items": [{"$skip": (page - 1) * page_size}, {"$limit": page_size}],
            "total": [{"$count": "n"}],
        }},
        {"$project": {"items": 1, "total": {"$ifNull": [{"$arrayElemAt": ["$total.n", 0]}, 0]} }},
    ]


def monthly_attendance_pipeline(emp_code: str, month: str):
    start, end = validate_month(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$addFields": {"_dateObj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lte": [{"$dayOfWeek": "$_dateObj"}, 6]}, {"$gte": [{"$dayOfWeek": "$_dateObj"}, 2]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = validate_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start.isoformat()]},
                    {"$lte": ["$date", end.isoformat()]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_logDate": {"$cond": [
            {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
            {"$dateFromString": {"dateString": "$logs.date"}}, None
        ]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": {"$ifNull": ["$_logDate", datetime(1970,1,1)]}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$ifNull": ["$_logDate", datetime(1970,1,1)]}}, 6]},
                ]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0
            ]}},
            "work_hours_values": {"$push": {"$cond": [
                {"$and": [{"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "avg_work_hours": {"$let": {"vars": {"vals": {"$filter": {"input": "$work_hours_values", "as": "v", "cond": {"$ne": ["$$v", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$vals"}, 0]}, {"$round": [{"$avg": "$$vals"}, 2]}, None]}}},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start, end = validate_month(month)
    pipe = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipe.append({"$match": {"employee.department": department}})
    pipe.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipe


def trend_pipeline(department: str, start_s: str, end_s: str):
    start = parse_date(start_s, "from")
    end = parse_date(end_s, "to")
    return [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department, "employee.joined_on": {"$lte": end_s}}},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": "$_d",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time(), UTC), datetime.combine(end + timedelta(days=1), time(), UTC)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "pipeline": [{"$match": {"department": department, "joined_on": {"$lte": end_s}}}, {"$project": {"_id": 0, "joined_on": 1}}], "as": "_employees"}},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": "$_employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$date"]}}}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
            "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


# -------------------------------- Endpoints --------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [clean_employee(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    ts = ts.replace(microsecond=0)
    local = local_dt(ts)
    rec_day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": rec_day.isoformat(), "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(ts, emp["shift_start"], emp["shift_end"], rec_day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for that date")
    return clean_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    ts = as_utc(ts)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"attendance record not found for {body.emp_code}"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail=f"attendance record for {body.emp_code} on {record.get('date')} is already punched out"
        )

    punch_in = record.get("punch_in")
    if punch_in is None:
        raise HTTPException(status_code=422, detail="attendance record has no punch-in time")

    punch_in = as_utc(punch_in)

    if ts <= punch_in or ts - punch_in > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours"
        )

    rec_day = parse_date(record["date"])
    hours = work_hours(punch_in, ts)
    ot = overtime_minutes(ts, emp["shift_start"], emp["shift_end"], rec_day)
    half = bool(hours < 4.50)

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": hours,
                "overtime_minutes": ot,
                "half_day": half
            }
        }
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record changed; retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return clean_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    result = list(db.attendance_logs.aggregate(attendance_pipeline(query, page, page_size)))
    facet = result[0] if result else {"items": [], "total": 0}
    return {"items": [clean_attendance(d) for d in facet["items"]], "total": facet["total"], "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    rec_day = parse_date(date)
    if not db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(404, "unknown employee")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not original:
        raise HTTPException(404, "attendance record not found")
    employee = db.employees.find_one({"emp_code": emp_code})
    current_status = original["status"]
    current_in = as_utc(original.get("punch_in"))
    current_out = as_utc(original.get("punch_out"))
    new_status = body.status if body.status is not None else current_status
    new_in = epoch_ms_to_dt(body.punch_in, "punch_in") if body.punch_in is not None else current_in
    new_out = epoch_ms_to_dt(body.punch_out, "punch_out") if body.punch_out is not None else current_out
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(new_in, employee["shift_start"], employee["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_out is not None:
            seconds = (new_out - new_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new_late = late_minutes(new_in, employee["shift_start"], employee["shift_end"], rec_day) if new_in else 0
    new_hours = work_hours(new_in, new_out) if new_in and new_out else None
    new_overtime = overtime_minutes(new_out, employee["shift_start"], employee["shift_end"], rec_day) if new_in and new_out else 0
    new_half = bool(new_hours is not None and new_hours < 4.50)
    final = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out,
        "work_hours": new_hours, "late_minutes": new_late,
        "overtime_minutes": new_overtime, "half_day": new_half,
    }
    changes = {}
    for key, new_val in final.items():
        old_val = original.get(key)
        if key == "late_minutes":
            old_val = original.get(key, 0)
        elif key == "overtime_minutes":
            old_val = original.get(key, 0)
        elif key == "half_day":
            old_val = original.get(key, False)
        elif key == "work_hours":
            old_val = original.get(key)
        if isinstance(old_val, datetime):
            old_val = as_utc(old_val)
        if old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: compare prior history length and key values; if another edit wins, return 409.
    history = original.get("history", [])
    query = {"_id": original["_id"], "history": history}
    for key in ("status", "punch_in", "punch_out"):
        query[key] = original.get(key)
    result = db.attendance_logs.update_one(query, {"$set": final, "$push": {"history": entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    updated = db.attendance_logs.find_one({"_id": original["_id"]})
    return clean_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = validate_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "unknown employee")
    joined = max(start, parse_date(employee["joined_on"], "joined_on"))
    days = working_days(joined, end)
    docs = list(db.attendance_logs.aggregate(monthly_attendance_pipeline(emp_code, month)))
    agg = docs[0] if docs else {}
    present = float(agg.get("present_days", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": days,
        "present_days": round_half_up(present, 2), "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)), "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": None if days == 0 else round_half_up(present / days * 100, 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    validate_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department), allowDiskUse=True))
    return {"month": month, "items": json_safe(items)}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start = parse_date(from_, "from")
    end = parse_date(to, "to")
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(trend_pipeline(department, from_, to), allowDiskUse=True))
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item.get(field) is not None:
                item[field] = round_half_up(item[field], 4)
    return {"department": department, "items": json_safe(items)}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": attendance_pipeline(q, page, page_size), "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        validate_month(month)
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": monthly_attendance_pipeline(emp_code, month), "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        explanation = db.command("explain", {"aggregate": "employees", "pipeline": department_summary_pipeline(month, department), "cursor": {}}, verbosity="executionStats")
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": trend_pipeline(department, from_, to), "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": json_safe(explanation)}

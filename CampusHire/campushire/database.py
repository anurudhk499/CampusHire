"""
database.py - MongoDB layer for CampusHire.

Everything database related lives here:
  * connection (PyMongo) and GridFS bucket (default bucket -> fs.files / fs.chunks)
  * collection handles: students, projects, drives, interests, admins
  * index creation (unique + compound + multikey) - runs automatically on start-up
  * aggregation pipelines used by the analytics dashboard and list pages
"""
import os
import re

import gridfs
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING, MongoClient
from werkzeug.security import generate_password_hash

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGO_DB", "campushire")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[DB_NAME]

# GridFS: files are split into chunks -> collections `fs.files` and `fs.chunks`
fs = gridfs.GridFS(db)

# Collections are created lazily by MongoDB on first write / index creation.
students = db["students"]
projects = db["projects"]
drives = db["drives"]
interests = db["interests"]
admins = db["admins"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_oid(value):
    """Convert a string to ObjectId, returning None when it is not valid."""
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def rx(text):
    """Case-insensitive, injection-safe regex filter for a user supplied string."""
    return {"$regex": re.escape(text.strip()), "$options": "i"}


# --------------------------------------------------------------------------- #
# Start-up: indexes, admin account, sample data
# --------------------------------------------------------------------------- #
def create_indexes():
    # UNIQUE indexes
    students.create_index([("usn", ASCENDING)], unique=True, name="uniq_usn")
    admins.create_index([("username", ASCENDING)], unique=True, name="uniq_admin_username")
    projects.create_index([("student_id", ASCENDING)], unique=True, name="uniq_project_per_student")

    # COMPOUND indexes
    interests.create_index(
        [("student_id", ASCENDING), ("drive_id", ASCENDING)],
        unique=True, name="uniq_student_drive",  # unique + compound: one interest per student per drive
    )
    students.create_index([("branch", ASCENDING), ("cgpa", DESCENDING)], name="branch_cgpa")
    drives.create_index([("status", ASCENDING), ("drive_date", ASCENDING)], name="status_date")
    drives.create_index([("company", ASCENDING), ("role", ASCENDING)], name="company_role")

    # Single-field / MULTIKEY indexes (indexes every element of an array)
    students.create_index([("skills", ASCENDING)], name="skills_multikey")
    projects.create_index([("technologies", ASCENDING)], name="technologies_multikey")
    interests.create_index([("drive_id", ASCENDING)], name="interest_drive")
    drives.create_index([("selected_students", ASCENDING)], name="selected_students_multikey")
    db["fs.files"].create_index([("metadata.type", ASCENDING)], name="gridfs_type")


def ensure_admin():
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "Admin@123")
    admins.update_one(
        {"username": username},
        {"$setOnInsert": {
            "username": username,
            "password_hash": generate_password_hash(password),
            "name": "Placement Officer",
        }},
        upsert=True,
    )


def init_db():
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - friendly start-up message
        raise SystemExit(
            f"\n[CampusHire] Cannot connect to MongoDB at {MONGO_URI}\n"
            f"Reason: {exc}\n"
            "Start MongoDB (e.g. `mongod`, or the MongoDB service) or set MONGO_URI to your Atlas connection string.\n"
        )
    create_indexes()
    ensure_admin()
    if (os.environ.get("SEED_SAMPLE_DATA", "1") == "1"
            and students.count_documents({}) == 0 and drives.count_documents({}) == 0):
        from seed import seed_database  # imported lazily to avoid a circular import
        seed_database()


# --------------------------------------------------------------------------- #
# Aggregation pipelines - analytics
# --------------------------------------------------------------------------- #
def students_by_branch():
    return list(students.aggregate([
        {"$group": {"_id": {"$ifNull": ["$branch", "Unspecified"]}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]))


def interest_by_company():
    """interests -> $lookup drives -> group by company."""
    return list(interests.aggregate([
        {"$lookup": {"from": "drives", "localField": "drive_id", "foreignField": "_id", "as": "drive"}},
        {"$unwind": "$drive"},
        {"$group": {"_id": "$drive.company", "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]))


def projects_by_technology():
    """$unwind the technologies array, then group case-insensitively."""
    return list(projects.aggregate([
        {"$unwind": "$technologies"},
        {"$group": {"_id": {"$toLower": "$technologies"}, "name": {"$first": "$technologies"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
        {"$limit": 12},
    ]))


def drive_status_split():
    return list(drives.aggregate([
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]))


def placed_count():
    """Distinct students selected in at least one drive (unwind + addToSet)."""
    res = list(drives.aggregate([
        {"$unwind": "$selected_students"},
        {"$group": {"_id": None, "placed": {"$addToSet": "$selected_students"}}},
        {"$project": {"_id": 0, "placed": {"$size": "$placed"}}},
    ]))
    return res[0]["placed"] if res else 0


def collection_counts():
    return {
        "students": students.count_documents({}),
        "projects": projects.count_documents({}),
        "drives": drives.count_documents({}),
        "interests": interests.count_documents({}),
    }


def gridfs_stats():
    files = db["fs.files"]
    by_type = list(files.aggregate([
        {"$group": {"_id": "$metadata.type", "count": {"$sum": 1}, "bytes": {"$sum": "$length"}}},
        {"$sort": {"_id": 1}},
    ]))
    return {
        "files": files.count_documents({}),
        "chunks": db["fs.chunks"].count_documents({}),
        "bytes": sum(t["bytes"] for t in by_type),
        "by_type": by_type,
    }


# --------------------------------------------------------------------------- #
# Aggregation pipelines - lists with joins
# --------------------------------------------------------------------------- #
def drive_query(company="", role="", status="upcoming"):
    """Regex search over company and role (case-insensitive, partial match)."""
    q = {}
    if company.strip():
        q["company"] = rx(company)
    if role.strip():
        q["role"] = rx(role)
    if status in ("upcoming", "completed"):
        q["status"] = status
    return q


def drives_with_counts(query=None, limit=0):
    """Drives + number of interested students via $lookup on `interests`."""
    pipeline = [
        {"$match": query or {}},
        {"$lookup": {"from": "interests", "localField": "_id", "foreignField": "drive_id", "as": "_ints"}},
        {"$addFields": {"interest_count": {"$size": "$_ints"}}},
        {"$project": {"_ints": 0}},
        {"$sort": {"drive_date": 1}},
    ]
    if limit:
        pipeline.append({"$limit": limit})
    return list(drives.aggregate(pipeline))


def completed_drives_with_selected(company=""):
    """Completed drives with selected student documents joined through an ObjectId array."""
    match = {"status": "completed"}
    if company.strip():
        match["company"] = rx(company)
    return list(drives.aggregate([
        {"$match": match},
        {"$lookup": {"from": "students", "localField": "selected_students",
                     "foreignField": "_id", "as": "selected"}},
        {"$project": {"company": 1, "role": 1, "package_lpa": 1, "drive_date": 1, "location": 1,
                      "selected._id": 1, "selected.name": 1, "selected.usn": 1, "selected.branch": 1,
                      "selected.photo_id": 1}},
        {"$sort": {"drive_date": -1}},
    ]))


def interested_students(drive_id):
    return list(interests.aggregate([
        {"$match": {"drive_id": drive_id}},
        {"$lookup": {"from": "students", "localField": "student_id", "foreignField": "_id", "as": "student"}},
        {"$unwind": "$student"},
        {"$sort": {"student.name": 1}},
    ]))


def student_interest_history(student_id):
    return list(interests.aggregate([
        {"$match": {"student_id": student_id}},
        {"$lookup": {"from": "drives", "localField": "drive_id", "foreignField": "_id", "as": "drive"}},
        {"$unwind": "$drive"},
        {"$sort": {"created_at": -1}},
    ]))


def search_students(name="", usn="", branch="", skills=""):
    """Regex search on name / USN / skills (array) plus exact branch filter."""
    conds = []
    if name.strip():
        conds.append({"name": rx(name)})
    if usn.strip():
        conds.append({"usn": rx(usn)})
    if branch.strip():
        conds.append({"branch": branch.strip()})
    for skill in [s for s in re.split(r"[,;]", skills) if s.strip()]:
        conds.append({"skills": rx(skill)})  # regex is applied to every array element
    return list(students.aggregate([
        {"$match": {"$and": conds} if conds else {}},
        {"$lookup": {"from": "projects", "localField": "_id", "foreignField": "student_id", "as": "project"}},
        {"$unwind": {"path": "$project", "preserveNullAndEmptyArrays": True}},
        {"$project": {"password_hash": 0}},
        {"$sort": {"name": 1}},
        {"$limit": 300},
    ]))


def projects_with_students(tech=""):
    match = {"technologies": rx(tech)} if tech.strip() else {}
    return list(projects.aggregate([
        {"$match": match},
        {"$lookup": {"from": "students", "localField": "student_id", "foreignField": "_id", "as": "student"}},
        {"$unwind": "$student"},
        {"$project": {"student.password_hash": 0}},
        {"$sort": {"created_at": -1}},
    ]))

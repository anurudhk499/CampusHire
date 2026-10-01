"""
CampusHire - Student Placement Management System
Flask + MongoDB (PyMongo) + GridFS

Run:  pip install -r requirements.txt
      python app.py
"""
import csv
import hmac
import io
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps

import gridfs
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

import database as dbm
from database import admins, drives, fs, interests, projects, students, to_oid

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "campushire-dev-secret-change-me"),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

BRANCHES = ["CSE", "AIML", "ISE", "ECE", "EEE", "ME", "CE"]
ROUND_TYPES = ["Online Test", "Coding Test", "Group Discussion", "Technical Interview", "HR Interview", "Other"]
ROUND_MODES = ["Online", "Virtual", "On-campus"]
USN_RE = re.compile(r"^[A-Z0-9]{6,15}$")
MAX_FILE_BYTES = 5 * 1024 * 1024
DUMMY_HASH = generate_password_hash("not-a-real-password")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, as PyMongo returns it


dbm.init_db()  # creates collections, indexes, admin account and sample data on first run


# --------------------------------------------------------------------------- #
# Security helpers: CSRF, redirects, headers
# --------------------------------------------------------------------------- #
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["BRANCHES"] = BRANCHES


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        token = session.get("_csrf", "")
        if not token or not hmac.compare_digest(token, sent):
            abort(400, "Your session expired or the form was invalid. Go back, refresh the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


@app.template_filter("fmtdate")
def fmtdate(value, fmt="%d %b %Y"):
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return value or ""


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(err):
    messages = {404: "We couldn't find that page.", 413: "The uploaded file is too large (max 12 MB)."}
    message = messages.get(err.code) or getattr(err, "description", "Something went wrong.")
    return render_template("error.html", code=err.code, message=message), err.code


# --------------------------------------------------------------------------- #
# Auth decorators
# --------------------------------------------------------------------------- #
def student_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        student = students.find_one({"_id": to_oid(session.get("student_id"))})
        if not student:
            session.pop("student_id", None)
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        g.student = student
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        admin = admins.find_one({"_id": to_oid(session.get("admin_id"))})
        if not admin:
            session.pop("admin_id", None)
            flash("Please sign in as a placement officer.", "info")
            return redirect(url_for("admin_login", next=request.full_path.rstrip("?")))
        g.admin = admin
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def split_list(text):
    seen, out = set(), []
    for part in re.split(r"[,;\n]", text or ""):
        item = part.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item[:40])
    return out[:20]


def check_eligibility(student, drive):
    min_cgpa = drive.get("min_cgpa") or 0
    cgpa = student.get("cgpa")
    if min_cgpa and cgpa is None:
        return False, "Add your CGPA to your profile to check eligibility."
    if min_cgpa and cgpa < min_cgpa:
        return False, f"Minimum CGPA required is {min_cgpa:.1f}."
    branches = drive.get("eligible_branches") or []
    if branches and student.get("branch") not in branches:
        return False, "This drive is not open to your branch."
    return True, ""


def sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_upload(file_storage, kind, owner_id, label):
    """Validate an upload by content (magic bytes) and store it in GridFS."""
    data = file_storage.read()
    if not data:
        raise ValueError("The selected file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Files must be smaller than 5 MB.")
    if kind == "resume":
        if not data.startswith(b"%PDF-"):
            raise ValueError("Resume must be a valid PDF file.")
        ctype = "application/pdf"
    else:
        ctype = sniff_image(data)
        if not ctype:
            raise ValueError("Images must be PNG, JPG or WEBP.")
    return fs.put(data, filename=label, content_type=ctype, metadata={"type": kind, "owner": owner_id})


def delete_file(file_id):
    if file_id:
        try:
            fs.delete(file_id)
        except Exception:
            pass


def parse_float(text, low, high, label, errors, required=False):
    text = (text or "").strip()
    if not text:
        if required:
            errors.append(f"{label} is required.")
        return None
    try:
        value = float(text)
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None
    if not low <= value <= high:
        errors.append(f"{label} must be between {low:g} and {high:g}.")
        return None
    return value


# --------------------------------------------------------------------------- #
# Public pages + student auth
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    stats = {"students": students.count_documents({}),
             "drives": drives.count_documents({"status": "upcoming"}),
             "placed": dbm.placed_count()}
    return render_template("index.html", stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        name = form.get("name", "").strip()
        usn = form.get("usn", "").strip().upper()
        branch = form.get("branch", "")
        password, confirm = form.get("password", ""), form.get("confirm", "")
        errors = []
        if len(name) < 3:
            errors.append("Enter your full name.")
        if not USN_RE.match(usn):
            errors.append("USN must be 6-15 letters or digits.")
        if branch not in BRANCHES:
            errors.append("Select your branch.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not errors:
            try:
                res = students.insert_one({
                    "usn": usn, "name": name, "branch": branch, "email": "", "phone": "", "cgpa": None,
                    "skills": [], "about": "", "photo_id": None, "resume_id": None,
                    "password_hash": generate_password_hash(password), "created_at": utcnow(),
                })
            except DuplicateKeyError:  # enforced by the unique index on students.usn
                errors.append("This USN is already registered. Try logging in.")
            else:
                session.clear()
                session["student_id"] = str(res.inserted_id)
                flash("Account created. Complete your profile to get noticed by recruiters.", "success")
                return redirect(url_for("edit_profile"))
        for e in errors:
            flash(e, "danger")
    return render_template("auth/register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usn = request.form.get("usn", "").strip().upper()
        student = students.find_one({"usn": usn})
        ok = check_password_hash(student["password_hash"] if student else DUMMY_HASH,
                                 request.form.get("password", ""))
        if student and ok:
            session.clear()
            session["student_id"] = str(student["_id"])
            return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
        flash("Incorrect USN or password.", "danger")
    return render_template("auth/login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# GridFS file serving
# --------------------------------------------------------------------------- #
@app.route("/file/<file_id>")
def serve_file(file_id):
    oid = to_oid(file_id)
    if not oid:
        abort(404)
    try:
        gf = fs.get(oid)
    except gridfs.errors.NoFile:
        abort(404)
    meta = gf.metadata or {}
    is_admin = bool(session.get("admin_id"))
    sid = session.get("student_id")
    if meta.get("type") == "resume":
        if not (is_admin or (sid and str(meta.get("owner")) == sid)):
            abort(403, "You can only open your own resume.")
    elif not (is_admin or sid):
        abort(403, "Log in to view this file.")
    resp = Response(gf.read(), mimetype=gf.content_type or "application/octet-stream")
    disposition = "attachment" if request.args.get("download") else "inline"
    resp.headers["Content-Disposition"] = f'{disposition}; filename="{gf.filename or "file"}"'
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# --------------------------------------------------------------------------- #
# Student area
# --------------------------------------------------------------------------- #
@app.route("/dashboard")
@student_required
def dashboard():
    s = g.student
    project = projects.find_one({"student_id": s["_id"]})
    interested_ids = set(interests.distinct("drive_id", {"student_id": s["_id"]}))
    upcoming = dbm.drives_with_counts({"status": "upcoming"}, limit=4)
    selected_in = list(drives.find({"selected_students": s["_id"]}, {"company": 1, "role": 1, "package_lpa": 1}))
    checklist = [
        ("Profile photo", bool(s.get("photo_id")), url_for("edit_profile")),
        ("PDF resume", bool(s.get("resume_id")), url_for("edit_profile")),
        ("CGPA and skills", bool(s.get("cgpa") is not None and s.get("skills")), url_for("edit_profile")),
        ("Project with screenshot", bool(project), url_for("my_project")),
    ]
    progress = int(100 * sum(1 for c in checklist if c[1]) / len(checklist))
    return render_template("student/dashboard.html", student=s, upcoming=upcoming, interested_ids=interested_ids,
                           selected_in=selected_in, checklist=checklist, progress=progress)


@app.route("/profile")
@student_required
def profile():
    s = g.student
    return render_template("student/profile.html", student=s, project=projects.find_one({"student_id": s["_id"]}),
                           history=dbm.student_interest_history(s["_id"]))


@app.route("/profile/edit", methods=["GET", "POST"])
@student_required
def edit_profile():
    s = g.student
    if request.method == "GET":
        return render_template("student/edit_profile.html", student=s)

    f = request.form
    errors = []
    name = f.get("name", "").strip()
    email = f.get("email", "").strip()
    phone = f.get("phone", "").strip()
    branch = f.get("branch", "")
    about = f.get("about", "").strip()[:600]
    skills = split_list(f.get("skills", ""))
    cgpa = parse_float(f.get("cgpa"), 0, 10, "CGPA", errors)
    if len(name) < 3:
        errors.append("Enter your full name.")
    if branch not in BRANCHES:
        errors.append("Select a valid branch.")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Enter a valid email address.")
    if phone and not re.match(r"^[0-9+\-\s]{10,15}$", phone):
        errors.append("Enter a valid phone number.")

    new_photo = new_resume = None
    if not errors:
        try:
            photo, resume = request.files.get("photo"), request.files.get("resume")
            if photo and photo.filename:
                new_photo = save_upload(photo, "photo", s["_id"], f"{s['usn']}_photo")
            if resume and resume.filename:
                new_resume = save_upload(resume, "resume", s["_id"], f"{s['usn']}_Resume.pdf")
        except ValueError as exc:
            delete_file(new_photo)
            errors.append(str(exc))

    if errors:
        for e in errors:
            flash(e, "danger")
        merged = dict(s, name=name, email=email, phone=phone, branch=branch, about=about, skills=skills, cgpa=cgpa)
        return render_template("student/edit_profile.html", student=merged)

    update = {"name": name, "email": email, "phone": phone, "branch": branch, "cgpa": cgpa,
              "skills": skills, "about": about, "updated_at": utcnow()}
    if new_photo:
        update["photo_id"] = new_photo
    if new_resume:
        update["resume_id"] = new_resume
    students.update_one({"_id": s["_id"]}, {"$set": update})
    if new_photo:
        delete_file(s.get("photo_id"))
    if new_resume:
        delete_file(s.get("resume_id"))
    flash("Profile updated.", "success")
    return redirect(url_for("profile"))


@app.route("/project", methods=["GET", "POST"])
@student_required
def my_project():
    s = g.student
    project = projects.find_one({"student_id": s["_id"]})
    if request.method == "GET":
        return render_template("student/project.html", project=project)

    f = request.form
    title = f.get("title", "").strip()
    description = f.get("description", "").strip()[:1000]
    technologies = split_list(f.get("technologies", ""))
    github = f.get("github_url", "").strip()
    shot = request.files.get("screenshot")
    has_shot = bool(shot and shot.filename)
    errors = []
    if len(title) < 3:
        errors.append("Enter a project title.")
    if len(description) < 10:
        errors.append("Describe your project in at least 10 characters.")
    if not technologies:
        errors.append("List at least one technology.")
    if github and not github.startswith(("http://", "https://")):
        errors.append("The repository link must start with http:// or https://.")
    if not project and not has_shot:
        errors.append("Upload a screenshot of your project.")
    new_shot = None
    if not errors and has_shot:
        try:
            new_shot = save_upload(shot, "screenshot", s["_id"], f"{s['usn']}_project")
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        for e in errors:
            flash(e, "danger")
        merged = dict(project or {}, title=title, description=description,
                      technologies=technologies, github_url=github)
        return render_template("student/project.html", project=merged)

    data = {"title": title, "description": description, "technologies": technologies,
            "github_url": github, "updated_at": utcnow()}
    if new_shot:
        data["screenshot_id"] = new_shot
    # one project per student: guaranteed by the unique index on projects.student_id
    projects.update_one({"student_id": s["_id"]}, {"$set": data, "$setOnInsert": {"created_at": utcnow()}},
                        upsert=True)
    if new_shot and project:
        delete_file(project.get("screenshot_id"))
    flash("Project saved.", "success")
    return redirect(url_for("profile"))


@app.route("/project/delete", methods=["POST"])
@student_required
def delete_project():
    project = projects.find_one_and_delete({"student_id": g.student["_id"]})
    if project:
        delete_file(project.get("screenshot_id"))
        flash("Project removed.", "info")
    return redirect(url_for("profile"))


@app.route("/drives")
@student_required
def drives_list():
    company = request.args.get("company", "")
    role = request.args.get("role", "")
    status = request.args.get("status", "upcoming")
    if status not in ("upcoming", "completed", "all"):
        status = "upcoming"
    items = dbm.drives_with_counts(dbm.drive_query(company, role, status))
    mine = set(interests.distinct("drive_id", {"student_id": g.student["_id"]}))
    for d in items:
        d["eligible"], d["reason"] = check_eligibility(g.student, d)
        d["interested"] = d["_id"] in mine
    return render_template("student/drives.html", items=items, company=company, role=role, status=status)


@app.route("/drives/<drive_id>")
@student_required
def drive_detail(drive_id):
    drive = drives.find_one({"_id": to_oid(drive_id)}) or abort(404)
    eligible, reason = check_eligibility(g.student, drive)
    interested = interests.count_documents({"student_id": g.student["_id"], "drive_id": drive["_id"]}) > 0
    selected = list(students.find({"_id": {"$in": drive.get("selected_students", [])}},
                                  {"name": 1, "usn": 1, "branch": 1, "photo_id": 1}))
    return render_template("student/drive_detail.html", drive=drive, eligible=eligible, reason=reason,
                           interested=interested, selected=selected,
                           interest_count=interests.count_documents({"drive_id": drive["_id"]}))


@app.route("/drives/<drive_id>/interest", methods=["POST"])
@student_required
def toggle_interest(drive_id):
    drive = drives.find_one({"_id": to_oid(drive_id)}) or abort(404)
    target = safe_next(request.form.get("next")) or url_for("drives_list")
    if request.form.get("action") == "remove":
        interests.delete_one({"student_id": g.student["_id"], "drive_id": drive["_id"]})
        flash(f"Removed your interest in {drive['company']}.", "info")
        return redirect(target)
    eligible, reason = check_eligibility(g.student, drive)
    if drive["status"] != "upcoming":
        flash("This drive is already completed.", "danger")
    elif not eligible:
        flash(reason, "danger")
    else:
        try:
            interests.insert_one({"student_id": g.student["_id"], "drive_id": drive["_id"],
                                  "created_at": utcnow()})
            flash(f"Marked as interested in {drive['company']}.", "success")
        except DuplicateKeyError:  # unique compound index (student_id, drive_id)
            flash("You have already marked interest in this drive.", "info")
    return redirect(target)


@app.route("/completed")
@student_required
def completed():
    company = request.args.get("company", "")
    return render_template("student/completed.html", items=dbm.completed_drives_with_selected(company),
                           company=company)


# --------------------------------------------------------------------------- #
# Admin: auth + dashboard
# --------------------------------------------------------------------------- #
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        admin = admins.find_one({"username": request.form.get("username", "").strip()})
        ok = check_password_hash(admin["password_hash"] if admin else DUMMY_HASH, request.form.get("password", ""))
        if admin and ok:
            session.clear()
            session["admin_id"] = str(admin["_id"])
            return redirect(safe_next(request.args.get("next")) or url_for("admin_dashboard"))
        flash("Incorrect username or password.", "danger")
    return render_template("auth/admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("admin_login"))


def series(rows, label="_id"):
    return {"labels": [str(r[label]) for r in rows], "values": [r["count"] for r in rows]}


@app.route("/admin")
@admin_required
def admin_dashboard():
    counts = dbm.collection_counts()
    gstats = dbm.gridfs_stats()
    charts = {
        "branch": series(dbm.students_by_branch()),
        "interest": series(dbm.interest_by_company()),
        "tech": series(dbm.projects_by_technology(), "name"),
        "status": series(dbm.drive_status_split()),
    }
    status = {l: v for l, v in zip(charts["status"]["labels"], charts["status"]["values"])}
    return render_template("admin/dashboard.html", counts=counts, gstats=gstats, charts=charts, status=status,
                           placed=dbm.placed_count(), recent=dbm.drives_with_counts({}, limit=0)[-6:][::-1])


# --------------------------------------------------------------------------- #
# Admin: students + projects
# --------------------------------------------------------------------------- #
@app.route("/admin/students")
@admin_required
def admin_students():
    q = {k: request.args.get(k, "") for k in ("name", "usn", "branch", "skills")}
    return render_template("admin/students.html", items=dbm.search_students(**q), q=q)


@app.route("/admin/students/<student_id>")
@admin_required
def admin_student_detail(student_id):
    s = students.find_one({"_id": to_oid(student_id)}, {"password_hash": 0}) or abort(404)
    return render_template("admin/student_detail.html", s=s, project=projects.find_one({"student_id": s["_id"]}),
                           history=dbm.student_interest_history(s["_id"]),
                           placed_in=list(drives.find({"selected_students": s["_id"]}, {"company": 1, "role": 1})))


@app.route("/admin/projects")
@admin_required
def admin_projects():
    tech = request.args.get("tech", "")
    return render_template("admin/projects.html", items=dbm.projects_with_students(tech), tech=tech)


# --------------------------------------------------------------------------- #
# Admin: placement drives (CRUD, interested students, publish results)
# --------------------------------------------------------------------------- #
def parse_drive_form(f):
    errors = []
    company, role = f.get("company", "").strip(), f.get("role", "").strip()
    if len(company) < 2:
        errors.append("Enter the company name.")
    if len(role) < 2:
        errors.append("Enter the job role.")
    package = parse_float(f.get("package_lpa"), 0, 500, "Package (LPA)", errors, required=True)
    min_cgpa = parse_float(f.get("min_cgpa"), 0, 10, "Minimum CGPA", errors) or 0.0
    try:
        drive_date = datetime.strptime(f.get("drive_date", ""), "%Y-%m-%d")
    except ValueError:
        drive_date = None
        errors.append("Choose a valid drive date.")
    rounds = []
    names, types = f.getlist("round_name"), f.getlist("round_type")
    modes, dates = f.getlist("round_mode"), f.getlist("round_date")
    for i, rname in enumerate(names):
        if rname.strip():
            rounds.append({  # embedded document
                "round_no": len(rounds) + 1, "name": rname.strip()[:80],
                "type": types[i] if i < len(types) and types[i] in ROUND_TYPES else "Other",
                "mode": modes[i] if i < len(modes) and modes[i] in ROUND_MODES else "On-campus",
                "date": dates[i] if i < len(dates) else "",
            })
    if not rounds:
        errors.append("Add at least one placement round.")
    status = f.get("status", "upcoming")
    doc = {
        "company": company, "role": role, "package_lpa": package, "min_cgpa": min_cgpa,
        "location": f.get("location", "").strip(), "description": f.get("description", "").strip()[:1500],
        "drive_date": drive_date, "eligible_branches": [b for b in f.getlist("branches") if b in BRANCHES],
        "rounds": rounds, "status": status if status in ("upcoming", "completed") else "upcoming",
    }
    return doc, errors


@app.route("/admin/drives")
@admin_required
def admin_drives():
    company = request.args.get("company", "")
    role = request.args.get("role", "")
    status = request.args.get("status", "all")
    return render_template("admin/drives.html", items=dbm.drives_with_counts(dbm.drive_query(company, role, status)),
                           company=company, role=role, status=status)


@app.route("/admin/drives/new", methods=["GET", "POST"])
@admin_required
def admin_drive_new():
    if request.method == "POST":
        doc, errors = parse_drive_form(request.form)
        if not errors:
            doc.update(selected_students=[], created_at=utcnow())
            drives.insert_one(doc)
            flash(f"Drive for {doc['company']} added.", "success")
            return redirect(url_for("admin_drives"))
        for e in errors:
            flash(e, "danger")
        return render_template("admin/drive_form.html", drive=doc, mode="new", round_types=ROUND_TYPES,
                               round_modes=ROUND_MODES)
    blank = {"status": "upcoming", "rounds": [], "eligible_branches": [], "min_cgpa": 0}
    return render_template("admin/drive_form.html", drive=blank, mode="new", round_types=ROUND_TYPES,
                           round_modes=ROUND_MODES)


@app.route("/admin/drives/<drive_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_drive_edit(drive_id):
    drive = drives.find_one({"_id": to_oid(drive_id)}) or abort(404)
    if request.method == "POST":
        doc, errors = parse_drive_form(request.form)
        if not errors:
            doc["updated_at"] = utcnow()
            drives.update_one({"_id": drive["_id"]}, {"$set": doc})
            flash("Drive updated.", "success")
            return redirect(url_for("admin_drives"))
        for e in errors:
            flash(e, "danger")
        drive = dict(drive, **doc)
    return render_template("admin/drive_form.html", drive=drive, mode="edit", round_types=ROUND_TYPES,
                           round_modes=ROUND_MODES)


@app.route("/admin/drives/<drive_id>/delete", methods=["POST"])
@admin_required
def admin_drive_delete(drive_id):
    oid = to_oid(drive_id) or abort(404)
    res = drives.delete_one({"_id": oid})
    if res.deleted_count:
        removed = interests.delete_many({"drive_id": oid}).deleted_count  # cascade
        flash(f"Drive deleted along with {removed} interest record(s).", "info")
    return redirect(url_for("admin_drives"))


@app.route("/admin/drives/<drive_id>/interested")
@admin_required
def admin_drive_interested(drive_id):
    drive = drives.find_one({"_id": to_oid(drive_id)}) or abort(404)
    rows = dbm.interested_students(drive["_id"])
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["USN", "Name", "Branch", "CGPA", "Email", "Phone", "Skills", "Marked on"])
        for r in rows:
            st = r["student"]
            w.writerow([st["usn"], st["name"], st.get("branch", ""), st.get("cgpa", ""), st.get("email", ""),
                        st.get("phone", ""), "; ".join(st.get("skills", [])), r["created_at"].strftime("%Y-%m-%d")])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="{drive["company"]}_interested.csv"'
        return resp
    return render_template("admin/interested.html", drive=drive, rows=rows)


@app.route("/admin/drives/<drive_id>/publish", methods=["GET", "POST"])
@admin_required
def admin_drive_publish(drive_id):
    drive = drives.find_one({"_id": to_oid(drive_id)}) or abort(404)
    rows = dbm.interested_students(drive["_id"])
    if request.method == "POST":
        allowed = {str(r["student"]["_id"]): r["student"]["_id"] for r in rows}
        chosen = [allowed[i] for i in request.form.getlist("selected") if i in allowed]
        drives.update_one({"_id": drive["_id"]}, {"$set": {
            "selected_students": chosen, "status": "completed", "published_at": utcnow()}})
        flash(f"Results published for {drive['company']}: {len(chosen)} student(s) selected.", "success")
        return redirect(url_for("admin_drives"))
    return render_template("admin/publish.html", drive=drive, rows=rows,
                           selected={str(x) for x in drive.get("selected_students", [])})


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))

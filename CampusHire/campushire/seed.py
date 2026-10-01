"""
seed.py - loads the sample data in ./sample_data into MongoDB (and GridFS).

Runs automatically on first start (when `students` and `drives` are empty).
Manual usage:
    python seed.py           # seed only if the database is empty
    python seed.py --reset   # DROP the CampusHire database contents and re-seed

Sample student login: any USN in sample_data/students.json, password  Student@123
"""
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

import database as dbm

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")
SAMPLE_PASSWORD = "Student@123"


def _load(name):
    with open(os.path.join(DATA_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def make_pdf(title, lines):
    """Build a small, valid one-page PDF so seeded resumes can be downloaded."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = [f"BT /F1 20 Tf 60 780 Td ({esc(title)}) Tj ET"]
    y = 750
    for line in lines:
        ops.append(f"BT /F1 12 Tf 60 {y} Td ({esc(line)}) Tj ET")
        y -= 22
    stream = "\n".join(ops).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _svg(inner):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200">' + inner + "</svg>").encode()


def avatar_svg(initials, hue):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><defs>'
            '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="hsl({hue},80%,62%)"/>'
            f'<stop offset="1" stop-color="hsl({(hue + 35) % 360},80%,42%)"/></linearGradient></defs>'
            '<rect width="200" height="200" fill="url(#g)"/>'
            '<text x="100" y="122" font-family="Arial,sans-serif" font-size="76" font-weight="700" '
            f'fill="#fff" text-anchor="middle">{initials}</text></svg>').encode()


def screenshot_svg(title, hue):
    return _svg(
        f'<rect width="320" height="200" fill="hsl({hue},70%,96%)"/>'
        f'<rect width="320" height="28" fill="hsl({hue},70%,45%)"/>'
        '<circle cx="14" cy="14" r="4" fill="#fff"/><circle cx="28" cy="14" r="4" fill="#fff" opacity=".7"/>'
        f'<rect x="18" y="46" width="130" height="120" rx="8" fill="hsl({hue},70%,88%)"/>'
        f'<rect x="164" y="46" width="138" height="52" rx="8" fill="hsl({hue},70%,80%)"/>'
        f'<rect x="164" y="112" width="138" height="54" rx="8" fill="hsl({hue},70%,90%)"/>'
        f'<text x="160" y="188" font-family="Arial,sans-serif" font-size="12" fill="hsl({hue},60%,25%)" '
        f'text-anchor="middle">{title}</text>'
    )


def reset_database():
    for name in ("students", "projects", "drives", "interests", "admins", "fs.files", "fs.chunks"):
        dbm.db[name].delete_many({})


def seed_database():
    rnd = random.Random(42)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = datetime(now.year, now.month, now.day)
    usn_to_id = {}

    for i, s in enumerate(_load("students")):
        doc = dict(s)
        doc["password_hash"] = generate_password_hash(SAMPLE_PASSWORD)
        doc["photo_id"] = None
        doc["resume_id"] = None
        doc["created_at"] = now - timedelta(days=rnd.randint(20, 90))
        sid = dbm.students.insert_one(doc).inserted_id
        usn_to_id[s["usn"]] = sid
        initials = "".join(w[0] for w in s["name"].split()[:2]).upper()
        photo = dbm.fs.put(avatar_svg(initials, (i * 47) % 360), filename=f"{s['usn']}_photo.svg",
                           content_type="image/svg+xml", metadata={"type": "photo", "owner": sid})
        resume_lines = [f"USN: {s['usn']}   Branch: {s['branch']}   CGPA: {s['cgpa']}",
                        f"Email: {s['email']}   Phone: {s['phone']}",
                        "Skills: " + ", ".join(s["skills"]), "", s["about"]]
        resume = dbm.fs.put(make_pdf(s["name"], resume_lines), filename=f"{s['usn']}_Resume.pdf",
                            content_type="application/pdf", metadata={"type": "resume", "owner": sid})
        dbm.students.update_one({"_id": sid}, {"$set": {"photo_id": photo, "resume_id": resume}})

    for i, p in enumerate(_load("projects")):
        sid = usn_to_id[p.pop("usn")]
        shot = dbm.fs.put(screenshot_svg(p["title"], (i * 61 + 200) % 360), filename=f"{p['title']}.svg",
                          content_type="image/svg+xml", metadata={"type": "screenshot", "owner": sid})
        p.update(student_id=sid, screenshot_id=shot, created_at=now - timedelta(days=rnd.randint(1, 30)))
        dbm.projects.insert_one(p)

    company_to_id = {}
    for d in _load("drives"):
        drive_date = today + timedelta(days=d["offset"])
        rounds = [{"round_no": n, "name": r["name"], "type": r["type"], "mode": r["mode"],
                   "date": (drive_date + timedelta(days=r["day_offset"])).strftime("%Y-%m-%d")}
                  for n, r in enumerate(d["rounds"], 1)]  # embedded documents
        doc = {
            "company": d["company"], "role": d["role"], "package_lpa": d["package_lpa"],
            "location": d["location"], "description": d["description"], "drive_date": drive_date,
            "status": d["status"], "min_cgpa": d["min_cgpa"], "eligible_branches": d["branches"],
            "rounds": rounds,
            "selected_students": [usn_to_id[u] for u in d.get("selected", [])],
            "created_at": now - timedelta(days=60),
        }
        if d["status"] == "completed":
            doc["published_at"] = drive_date + timedelta(days=5)
        company_to_id[d["company"]] = dbm.drives.insert_one(doc).inserted_id

    for it in _load("interests"):
        dbm.interests.insert_one({
            "student_id": usn_to_id[it["usn"]], "drive_id": company_to_id[it["company"]],
            "created_at": now - timedelta(days=rnd.randint(1, 40)),
        })
    print("[CampusHire] Sample data loaded. Student login: <USN> / " + SAMPLE_PASSWORD)


if __name__ == "__main__":
    dbm.init_db()
    if "--reset" in sys.argv:
        reset_database()
        dbm.ensure_admin()
        seed_database()
    elif dbm.students.count_documents({}) == 0:
        seed_database()
    else:
        print("Database already has data. Use `python seed.py --reset` to wipe and re-seed.")

# CampusHire - Student Placement Management System

ADBMS mini project built with **Flask + MongoDB (PyMongo) + GridFS**.
Students register with a USN, upload a photo, PDF resume and project, browse placement drives and mark interest.
The placement officer manages drives, searches students, downloads resumes, publishes results and sees live analytics.

## 1. Requirements
* Python 3.9+
* MongoDB running locally on the default port (`mongodb://localhost:27017`), or a MongoDB Atlas connection string

## 2. Installation
```bash
# 1. (optional) create a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux

# 2. install dependencies
pip install -r requirements.txt

# 3. make sure MongoDB is running (mongod / MongoDB service), then start the app
python app.py
```
Open **http://127.0.0.1:5000**

On the first run the app automatically:
* creates the `campushire` database, the collections and all indexes,
* creates the officer account,
* loads the sample data from `sample_data/` (students, projects, drives with rounds, interests) and stores
  sample photos, project screenshots and PDF resumes in GridFS.

> Do not `pip install gridfs`. GridFS ships with PyMongo.

## 3. Logins
| Role | Username / USN | Password |
|---|---|---|
| Placement officer | `admin` | `Admin@123` |
| Sample student | `CH22AI001` (any USN in `sample_data/students.json`) | `Student@123` |

New students can register from the Register page.

## 4. Configuration (optional environment variables)
| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB / Atlas connection string |
| `MONGO_DB` | `campushire` | Database name |
| `SECRET_KEY` | dev value | Flask session key. **Set your own for any real deployment** |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `Admin@123` | Officer account created on first run |
| `SEED_SAMPLE_DATA` | `1` | Set to `0` to start with an empty database |

Reset the sample data at any time: `python seed.py --reset`

## 5. Folder structure
```
campushire/
|-- app.py              Flask routes, auth, uploads, validation
|-- database.py         MongoDB connection, GridFS, indexes, aggregation pipelines
|-- seed.py             Loads sample_data/ into MongoDB and GridFS
|-- requirements.txt
|-- sample_data/        students.json, projects.json, drives.json, interests.json
|-- templates/          Jinja templates (auth/, student/, admin/)
`-- static/             css/style.css, js/main.js
```

## 6. ADBMS concepts and where they are implemented
| Concept | Implementation |
|---|---|
| Multiple collections | `students`, `projects`, `drives`, `interests` (+ `admins`) |
| GridFS | `fs.files` / `fs.chunks` store profile photos, project screenshots and PDF resumes (`save_upload`, `serve_file` in `app.py`). Uploads are validated by content (magic bytes), not only file extension |
| ObjectId relationships | `projects.student_id -> students._id`, `interests.student_id`, `interests.drive_id`, `drives.selected_students[] -> students._id`, `students.photo_id / resume_id -> fs.files._id` |
| Embedded documents | `drives.rounds[]` holds each placement round as a sub-document (`round_no, name, type, mode, date`) |
| Aggregation pipelines | `database.py`: `$group`, `$lookup`, `$unwind`, `$addFields`, `$project`, `$sort`, `$limit`, `$addToSet`, `$size`, `$toLower`, `$ifNull` |
| Regex search | Company / role / student name / USN / skills / technology, using `$regex` with `$options: "i"` on escaped input (`rx()` in `database.py`). Skills are matched against every element of an array |
| Unique indexes | `students.usn`, `admins.username`, `projects.student_id` (one project per student) |
| Compound indexes | `interests(student_id, drive_id)` **unique**, `students(branch, cgpa)`, `drives(status, drive_date)`, `drives(company, role)` |
| Multikey indexes | `students.skills`, `projects.technologies`, `drives.selected_students` |
| Referential clean-up | Deleting a drive also deletes its `interests` (`delete_many`) |

### Analytics dashboard (aggregations)
* Students by branch: `students` -> `$group`
* Interest by company: `interests` -> `$lookup drives` -> `$unwind` -> `$group`
* Projects by technology: `projects` -> `$unwind technologies` -> `$group` (case-insensitive)
* Upcoming vs completed: `drives` -> `$group` on `status`
* Students placed: `drives` -> `$unwind selected_students` -> `$group $addToSet` -> `$size`
* Collection counts and GridFS file count (`fs.files`, `fs.chunks`, size by file type via `$group`)

### Useful `mongosh` commands for your viva
```js
use campushire
show collections
db.getCollectionNames()
db.students.getIndexes()
db.interests.getIndexes()
db.fs.files.find({}, {filename: 1, length: 1, "metadata.type": 1})
db.fs.chunks.countDocuments()

// students whose skills match a regex
db.students.find({ skills: { $regex: "py", $options: "i" } }, { name: 1, usn: 1, skills: 1 })

// interested students per company
db.interests.aggregate([
  { $lookup: { from: "drives", localField: "drive_id", foreignField: "_id", as: "drive" } },
  { $unwind: "$drive" },
  { $group: { _id: "$drive.company", interested: { $sum: 1 } } },
  { $sort: { interested: -1 } }
])

// selected students for completed drives
db.drives.aggregate([
  { $match: { status: "completed" } },
  { $lookup: { from: "students", localField: "selected_students", foreignField: "_id", as: "selected" } },
  { $project: { company: 1, "selected.name": 1, "selected.usn": 1 } }
])

// the unique compound index rejects a duplicate interest
db.interests.explain("executionStats").find({ drive_id: db.drives.findOne()._id })
```

## 7. Features
**Student:** register with USN + hashed password, login, profile photo, PDF resume, one project with screenshot, view/edit profile,
browse and search drives (company, role, status), eligibility check (CGPA and branch), mark or withdraw interest, view completed drives and selected students.

**Placement officer:** secure login, analytics dashboard, search students (name, USN, branch, skills), resume download and preview,
view projects (filter by technology), add / edit / delete drives with embedded rounds, view interested students (CSV export), publish selected students.

## 8. Security notes
Passwords use Werkzeug salted hashes. Every POST form carries a CSRF token. Resumes are only visible to their owner and the officer.
Uploads are size limited (5 MB) and checked by file signature. Search input is regex-escaped to prevent regex injection.

## 9. Troubleshooting
* `Cannot connect to MongoDB`: start `mongod` or set `MONGO_URI`.
* The page looks unstyled: Bootstrap, Bootstrap Icons, Chart.js and Poppins load from CDNs, so an internet connection is needed.
* Port 5000 busy: `set PORT=5001` (Windows) or `PORT=5001 python app.py`.

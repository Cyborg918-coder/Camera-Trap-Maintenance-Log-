# Camera Trap Maintenance Log

**English** · [简体中文](README.zh-CN.md)

An **offline-first** maintenance log for camera-trap wildlife surveys — record in the field
with no signal, then sync the whole team's data automatically once you are back online.

> Built for and used in a real camera-trap survey in the mountains around Beijing:
> 100+ camera traps, several field operators, and no cell coverage at the survey sites.

---

## Why this exists

Camera-trap surveys share a set of everyday maintenance problems:

- **No cell signal in the field** — online spreadsheets and forms simply will not open
- **Paper logs** get lost, get skipped, or come back illegible
- **Progress is invisible** — nobody can tell which cameras are due for a check, or who last visited them
- **Photos and coordinates do not line up** with the right camera

This system puts the maintenance log on the phone: offline-capable, photo and GPS capture,
automatic aggregation, and a camera roster that stays current.

---

## Features

### Offline-first
- PWA + Service Worker caching — open it once while you have a connection, then use it with none
- Records are written to `localStorage` first and uploaded automatically once a network appears

### Maintenance records
- Camera ID + operator + operation type (multi-select) + free-text note
- Automatic GPS capture (needs a secure context — see [HTTPS is required](#https-is-required))
- Field photos, auto-compressed via `canvas` before storage
- KML track upload, as exported from apps such as TwoStepRoute

### Team collaboration
- FastAPI + SQLite backend aggregates everyone's records
- **Automatic two-way sync** — uploads this device's unsynced records and pulls in everybody else's
- The camera roster keeps itself current: which camera was last maintained, by whom, and when

### Access control
- **Access password** (HTTP Basic Auth) — for field team members: view and record
- **Admin password** — required separately for editing and deleting; team members never see those controls
- **Recycle bin** (soft delete) — deletions are recoverable, and permanent deletion needs confirmation

### Routes
- Named survey routes (e.g. `MTG North Line`) with an explicit camera order along each one
- One KML per route, with **version history** — the last 3 versions are kept and any of them can be restored

### Bandwidth-conscious by design
- **Lazy photo loading** — the list endpoint returns text fields only; a photo is fetched when you open that record
- `GET /api/record/{rid}` returns one full record, including the base64 photo

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Single-file HTML + vanilla JS — no framework, no build step |
| Backend | FastAPI + SQLite (WAL) + uvicorn |
| Deployment | systemd + self-signed TLS |
| Offline | Service Worker + Cache API + localStorage |

No build tooling, no bundler, no npm. Edit `index.html` and reload.

---

## Quick start

### Backend

```bash
python -m venv venv
venv/bin/pip install fastapi uvicorn

# Passwords are read from the environment only — never hard-coded
export IRCAM_USER=team
export IRCAM_PASS=your_password
export IRCAM_ADMIN_PASS=your_admin_password

venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

The database (`ircam.db`) and its tables are created automatically on first start.

### Frontend

`index.html` is a single-file app — put it in any static directory, or let the backend serve it.

When the frontend and backend share an origin, no configuration is needed: the frontend's
`DEFAULT_SERVER = ''` means "same origin".

---

## Configuration

### Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `IRCAM_USER` | Access username | `team` |
| `IRCAM_PASS` | Access password — for field team members | **unset** ⚠️ |
| `IRCAM_ADMIN_PASS` | Admin password — for edit / delete | **unset** ⚠️ |
| `QUOTE_USER` | Username for the `/quote/*` path only | falls back to `IRCAM_USER` |
| `QUOTE_PASS` | Password for the `/quote/*` path only | falls back to `IRCAM_PASS` |

**Security behaviour:** if `IRCAM_PASS` is not set, the server generates a random password at
startup and prints it, rather than falling back to a publicly known default. The system boots
**closed**, never open — there is no default password to forget to change.

`QUOTE_USER` / `QUOTE_PASS` exist so a public-facing page can live under the same server with a
different, separately shareable password.

### Production deployment (systemd)

```ini
[Unit]
Description=Camera Trap Maintenance Log
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ircam
Environment=IRCAM_USER=team
Environment=IRCAM_PASS=your_password
Environment=IRCAM_ADMIN_PASS=your_admin_password
# Needed only to bind a privileged port such as 80:
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ExecStart=/path/to/venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

For HTTPS, append to `ExecStart`:

```
--ssl-keyfile /path/to/key.pem --ssl-certfile /path/to/cert.pem
```

---

## HTTPS is required

Two features need a **secure context** and will silently refuse to work over plain HTTP:

- **Service Worker** (offline caching) → the page will not open offline in the field
- **Geolocation** (GPS) → position capture fails

A self-signed certificate is the only option when you have no domain name:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=YOUR_IP" \
  -addext "subjectAltName=IP:YOUR_IP"
```

Every field operator then trusts that certificate once on their phone. The steps differ between
Android and iOS — on iOS you must additionally enable it under
*Settings → General → About → Certificate Trust Settings*.

---

## API reference

All `/api/*` routes require the access password. Routes marked **admin** additionally require the
admin password in the `x-admin-pass` header.

### Records

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/records` | Record list (text fields only, `has_photo` flag) | access |
| `POST` | `/api/records` | Upload records (batch supported) | access |
| `GET` | `/api/record/{rid}` | One full record, including photo / KML | access |
| `PATCH` | `/api/record/{rid}` | Edit a record | admin |
| `DELETE` | `/api/record/{rid}` | Soft delete → recycle bin | admin |
| `GET` | `/api/deleted-record-ids` | IDs of soft-deleted records (for client-side sync) | access |

### Recycle bin

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/trash` | List deleted records | admin |
| `POST` | `/api/restore/{rid}` | Restore from the recycle bin | admin |
| `DELETE` | `/api/purge/{rid}` | Permanently delete (irreversible) | admin |

### Cameras

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/cameras` | Camera roster | access |
| `POST` | `/api/cameras` | Bulk upsert from a `cameras` array — updates coordinates and status | admin |
| `POST` | `/api/camera` | Create / update one camera | admin |
| `POST` | `/api/cameras/bulk` | Bulk-import new camera IDs (existing IDs are skipped) | admin |
| `DELETE` | `/api/camera/{code}` | Remove a camera (its records are kept) | admin |
| `GET` | `/api/camera/{code}/route` | Which route a camera belongs to | access |

### Routes

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/routes` | Route list | access |
| `POST` | `/api/routes` | Create a route | access |
| `GET` | `/api/routes/{rid}` | Route detail (without the KML body) | access |
| `PATCH` | `/api/routes/{rid}` | Rename / re-note a route | admin |
| `DELETE` | `/api/routes/{rid}` | Soft-delete a route | admin |
| `GET` | `/api/routes-map` | Camera → route mapping, for the roster page | access |
| `GET` | `/api/routes/{rid}/kml` | Download the current KML | access |
| `POST` | `/api/routes/{rid}/kml` | Replace the KML (the previous version is archived) | access |
| `GET` | `/api/routes/{rid}/history` | KML version history | access |
| `GET` | `/api/routes/{rid}/history/{hid}` | Download one historical version | access |
| `POST` | `/api/routes/{rid}/restore/{hid}` | Restore a historical version as current | access |

### Operators

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/operators` | Operator list (populates the entry form) | access |
| `POST` | `/api/operators` | Add an operator | admin |
| `DELETE` | `/api/operators/{name}` | Remove an operator (past records are unaffected) | admin |

### Stats & export

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/stats` | Summary counters | access |
| `GET` | `/api/export.csv` | Export records as CSV | access |
| `GET` | `/api/export.json` | Full JSON export (backup) | access |

### Static

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | The single-file frontend |
| `GET` | `/sw.js` | Service Worker |
| `GET` | `/manifest.json` | PWA manifest |

---

## Database schema

```sql
records (
  id TEXT PRIMARY KEY,
  ts TEXT, operator TEXT, code TEXT,
  ops TEXT,              -- JSON array of operation types
  lat REAL, lng REAL, note TEXT,
  photo TEXT,            -- base64
  kml_name TEXT, kml_content TEXT,
  created_at TEXT
)

cameras (
  code TEXT PRIMARY KEY,
  status TEXT DEFAULT 'active',   -- active | removed
  lat REAL, lng REAL,
  last_maint TEXT, last_by TEXT,
  updated_at TEXT
)

operators (
  name TEXT PRIMARY KEY,
  created_at TEXT, created_by TEXT
)

routes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kml_content TEXT, kml_name TEXT,
  kml_updated_at TEXT, kml_updated_by TEXT,
  note TEXT,
  created_at TEXT, created_by TEXT,
  deleted INTEGER DEFAULT 0, deleted_at TEXT
)

route_cameras (
  route_id TEXT, code TEXT, seq INTEGER,
  PRIMARY KEY (route_id, code)
)

route_kml_history (
  hid INTEGER PRIMARY KEY AUTOINCREMENT,
  route_id TEXT, kml_content TEXT, kml_name TEXT,
  saved_at TEXT, saved_by TEXT
)

purged_records (
  id TEXT PRIMARY KEY, purged_at TEXT
)
```

Tables are created on first start. Existing databases are migrated in place — `init_db()` adds the
soft-delete columns (`deleted`, `deleted_at`, `deleted_by`) to `records` if they are missing.

---

## Project structure

```
ircam/
├── index.html      # Single-file frontend (entry / records / roster / settings)
├── server.py       # FastAPI backend + SQLite
├── sw.js           # Service Worker (offline cache)
├── manifest.json   # PWA manifest
├── LICENSE         # MIT
├── .gitignore
└── README.md
```

> Runtime artifacts — `venv/`, `ircam.db`, `cert/`, and any site-specific deploy script — are
> **excluded by `.gitignore`**. Survey data, certificates and deployment credentials must never
> be committed.

---

## Notes for contributors

- **Field data and credentials never enter the repository.** `.gitignore` enforces this; please
  keep it that way rather than relying on remembering.
- Passwords are read from the environment only. Do not add a hard-coded fallback default —
  booting with a known password is worse than failing to boot.
- `index.html` is intentionally dependency-free. Please do not introduce a build step.

---

## License

[MIT](LICENSE)

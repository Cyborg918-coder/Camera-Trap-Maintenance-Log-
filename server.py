#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红外相机维护日志 - 后端服务
=============================
技术: FastAPI + SQLite (标准库sqlite3, 无ORM依赖)
功能: 维护记录同步 / 相机台账 / 导出CSV

部署: uvicorn server:app --host 0.0.0.0 --port 8080
依赖: pip install fastapi uvicorn
"""
import sqlite3
import json
import csv
import io
import os
import base64
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DB = BASE / "ircam.db"

app = FastAPI(title="红外相机维护日志")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---------- 访问口令（内部系统：防止外人点开就看到）----------
# 口令必须通过环境变量提供（见 deploy/ 下的 systemd 配置示例）
# 代码里只留占位符，不写真实口令
AUTH_USER = os.environ.get("IRCAM_USER", "team")
AUTH_PASS = os.environ.get("IRCAM_PASS", "CHANGE_ME")
# 管理员口令：修改/删除记录需要（与访问口令分开，避免队员误删）
ADMIN_PASS = os.environ.get("IRCAM_ADMIN_PASS", "CHANGE_ME_ADMIN")

@app.middleware("http")
async def basic_auth_mw(request: Request, call_next):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            u, p = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
            if u == AUTH_USER and p == AUTH_PASS:
                return await call_next(request)
        except Exception:
            pass
    return Response("需要登录才能访问（内部系统）", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="IRCAM"'})


def is_admin(request: Request) -> bool:
    """管理员校验：前端在设置页解锁后，请求头带 x-admin-pass"""
    return request.headers.get("x-admin-pass", "") == ADMIN_PASS

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        ts TEXT, operator TEXT, code TEXT,
        ops TEXT, lat REAL, lng REAL, note TEXT,
        photo TEXT, kml_name TEXT, kml_content TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS cameras (
        code TEXT PRIMARY KEY,
        status TEXT DEFAULT 'active',
        lat REAL, lng REAL,
        last_maint TEXT, last_by TEXT,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rec_code ON records(code);
    CREATE INDEX IF NOT EXISTS idx_rec_ts ON records(ts);
    """)
    # 迁移：为已有数据库补上"回收站"相关字段（软删除）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
    for col, ddl in [
        ("deleted",    "ALTER TABLE records ADD COLUMN deleted INTEGER DEFAULT 0"),
        ("deleted_at", "ALTER TABLE records ADD COLUMN deleted_at TEXT"),
        ("deleted_by", "ALTER TABLE records ADD COLUMN deleted_by TEXT"),
    ]:
        if col not in cols:
            conn.execute(ddl)
    conn.commit(); conn.close()

init_db()

# ---------- 前端静态文件 ----------
@app.get("/")
def index():
    return FileResponse(BASE / "index.html")

@app.get("/sw.js")
def sw():
    return FileResponse(BASE / "sw.js", media_type="application/javascript")

@app.get("/manifest.json")
def manifest():
    return FileResponse(BASE / "manifest.json", media_type="application/manifest+json")

# ---------- API ----------
@app.post("/api/records")
async def post_records(req: Request):
    """接收维护记录(支持批量)"""
    data = await req.json()
    recs = data.get("records", [])
    conn = db()
    accepted = []
    for r in recs:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO records
                   (id, ts, operator, code, ops, lat, lng, note, photo, kml_name, kml_content, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.get("id"), r.get("ts"), r.get("operator"), r.get("code"),
                 json.dumps(r.get("ops", []), ensure_ascii=False),
                 r.get("lat"), r.get("lng"), r.get("note"),
                 r.get("photo"), (r.get("kml") or {}).get("name"),
                 (r.get("kml") or {}).get("content"),
                 datetime.now().isoformat())
            )
            # 同步更新相机台账
            code = r.get("code")
            ops = r.get("ops", [])
            if code and not code.startswith("("):
                row = conn.execute("SELECT code FROM cameras WHERE code=?", (code,)).fetchone()
                status = "removed" if "移除相机" in ops else "active"
                move = ("挪动位置" in ops or "新增布设" in ops)
                lat = r.get("lat") if move else None
                lng = r.get("lng") if move else None
                if row:
                    if lat is not None:
                        conn.execute("UPDATE cameras SET status=?, lat=?, lng=?, last_maint=?, last_by=?, updated_at=? WHERE code=?",
                                     (status, lat, lng, (r.get("ts") or "")[:10], r.get("operator"),
                                      datetime.now().isoformat(), code))
                    else:
                        conn.execute("UPDATE cameras SET status=?, last_maint=?, last_by=?, updated_at=? WHERE code=?",
                                     (status, (r.get("ts") or "")[:10], r.get("operator"),
                                      datetime.now().isoformat(), code))
                else:
                    conn.execute("INSERT INTO cameras (code,status,lat,lng,last_maint,last_by,updated_at) VALUES (?,?,?,?,?,?,?)",
                                 (code, status, lat, lng, (r.get("ts") or "")[:10], r.get("operator"),
                                  datetime.now().isoformat()))
            accepted.append(r.get("id"))
        except Exception as e:
            print(f"记录保存失败 {r.get('id')}: {e}")
    conn.commit(); conn.close()
    return {"ok": True, "accepted": accepted, "count": len(accepted)}

@app.get("/api/records")
def get_records(code: str = None, operator: str = None, limit: int = 500):
    conn = db()
    # has_photo / has_kml 标记：告知前端"服务器上有照片/轨迹"，但不传输内容（省流量）
    q = ("SELECT id, ts, operator, code, ops, lat, lng, note, kml_name, created_at, "
         "CASE WHEN photo IS NOT NULL THEN 1 ELSE 0 END AS has_photo, "
         "CASE WHEN kml_content IS NOT NULL THEN 1 ELSE 0 END AS has_kml FROM records")
    where, args = ["COALESCE(deleted,0)=0"], []   # 回收站里的记录不返回
    if code: where.append("code=?"); args.append(code)
    if operator: where.append("operator=?"); args.append(operator)
    q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    conn.close()
    for r in rows:
        try: r["ops"] = json.loads(r["ops"])
        except: r["ops"] = []
    return {"records": rows, "count": len(rows)}


@app.get("/api/record/{rid}")
def get_one_record(rid: str):
    """单条记录完整内容（含照片/KML）—— 前端点开照片时才拉，避免一次下载几十MB"""
    conn = db()
    r = conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not r:
        return {"ok": False, "error": "not found"}
    d = dict(r)
    try: d["ops"] = json.loads(d["ops"])
    except: d["ops"] = []
    return {"ok": True, "record": d}


@app.delete("/api/record/{rid}")
def delete_record(rid: str, request: Request):
    """软删除：移入回收站（需要管理员口令）。可用 /api/restore/{rid} 恢复"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    cur = conn.execute(
        "UPDATE records SET deleted=1, deleted_at=?, deleted_by=? "
        "WHERE id=? AND COALESCE(deleted,0)=0",
        (datetime.now().isoformat(), request.headers.get("x-admin-name", "管理员"), rid))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "trashed": rid, "count": n}


@app.get("/api/trash")
def list_trash(request: Request):
    """回收站列表（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, ts, operator, code, ops, note, deleted_at, deleted_by FROM records "
        "WHERE COALESCE(deleted,0)=1 ORDER BY deleted_at DESC").fetchall()]
    conn.close()
    for r in rows:
        try: r["ops"] = json.loads(r["ops"])
        except: r["ops"] = []
    return {"ok": True, "records": rows, "count": len(rows)}


@app.post("/api/restore/{rid}")
def restore_record(rid: str, request: Request):
    """从回收站恢复（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    cur = conn.execute(
        "UPDATE records SET deleted=0, deleted_at=NULL, deleted_by=NULL WHERE id=?", (rid,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "restored": rid}


@app.delete("/api/purge/{rid}")
def purge_record(rid: str, request: Request):
    """从回收站彻底删除（不可恢复，需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    cur = conn.execute("DELETE FROM records WHERE id=? AND COALESCE(deleted,0)=1", (rid,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "purged": rid}


@app.patch("/api/record/{rid}")
def update_record(rid: str, payload: dict, request: Request):
    """修改一条记录（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    fields, args = [], []
    for k in ("note", "operator", "lat", "lng", "ts"):
        if k in payload:
            fields.append(f"{k}=?"); args.append(payload[k])
    if "ops" in payload:
        ops = payload["ops"]
        fields.append("ops=?")
        args.append(json.dumps(ops, ensure_ascii=False) if isinstance(ops, list) else ops)
    if not fields:
        conn.close()
        return {"ok": False, "error": "nothing to update"}
    args.append(rid)
    cur = conn.execute(f"UPDATE records SET {', '.join(fields)} WHERE id=?", args)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "updated": rid, "count": n}


@app.delete("/api/camera/{code}")
def delete_camera(code: str, request: Request):
    """删除一台相机（需要管理员口令；记录不删，只从台账移除）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    cur = conn.execute("DELETE FROM cameras WHERE code=?", (code,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "deleted": code, "count": n}


@app.post("/api/camera")
def upsert_camera(payload: dict, request: Request):
    """新增/更新一台相机（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    code = (payload.get("code") or "").strip().upper()
    if not code:
        return {"ok": False, "error": "code required"}
    conn = db()
    conn.execute(
        "INSERT INTO cameras (code, status, lat, lng, last_maint, last_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET status=excluded.status, lat=excluded.lat, "
        "lng=excluded.lng, updated_at=excluded.updated_at",
        (code, payload.get("status", "active"), payload.get("lat"), payload.get("lng"),
         payload.get("last_maint"), payload.get("last_by"), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return {"ok": True, "code": code}


@app.post("/api/cameras/bulk")
def bulk_cameras(payload: dict, request: Request):
    """批量导入相机编号（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    codes = payload.get("codes") or []
    if not isinstance(codes, list):
        return {"ok": False, "error": "codes must be a list"}
    conn = db()
    added = 0
    for raw in codes:
        c = str(raw).strip().upper()
        if not c:
            continue
        cur = conn.execute(
            "INSERT INTO cameras (code, status, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(code) DO NOTHING",
            (c, payload.get("status", "active"), datetime.now().isoformat()))
        added += cur.rowcount
    conn.commit()
    conn.close()
    return {"ok": True, "added": added, "total": len(codes)}

@app.get("/api/cameras")
def get_cameras():
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cameras ORDER BY code").fetchall()]
    conn.close()
    return {"cameras": rows, "count": len(rows)}

@app.post("/api/cameras")
async def post_cameras(req: Request):
    """批量导入/更新相机台账"""
    data = await req.json()
    cams = data.get("cameras", [])
    conn = db()
    n = 0
    for c in cams:
        conn.execute("""INSERT INTO cameras (code,status,lat,lng,updated_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET status=excluded.status,
                        lat=COALESCE(excluded.lat, cameras.lat), lng=COALESCE(excluded.lng, cameras.lng),
                        updated_at=excluded.updated_at""",
                     (c.get("code"), c.get("status", "active"), c.get("lat"), c.get("lng"),
                      datetime.now().isoformat()))
        n += 1
    conn.commit(); conn.close()
    return {"ok": True, "count": n}

@app.get("/api/stats")
def stats():
    conn = db()
    total_cams = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM cameras WHERE status='active'").fetchone()[0]
    total_recs = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    by_op = [dict(r) for r in conn.execute(
        "SELECT operator, COUNT(*) as n, MAX(ts) as last_ts FROM records GROUP BY operator ORDER BY n DESC").fetchall()]
    conn.close()
    return {"cameras_total": total_cams, "cameras_active": active,
            "records_total": total_recs, "by_operator": by_op}

@app.get("/api/export.csv")
def export_csv():
    """导出维护记录CSV(Excel可打开)"""
    conn = db()
    rows = conn.execute("SELECT ts, operator, code, ops, lat, lng, note FROM records ORDER BY ts DESC").fetchall()
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["时间", "维护人", "相机编号", "操作", "纬度", "经度", "备注"])
    for r in rows:
        try: ops = "/".join(json.loads(r["ops"]))
        except: ops = r["ops"]
        w.writerow([r["ts"], r["operator"], r["code"], ops, r["lat"], r["lng"], r["note"]])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=ircam_records.csv"})

@app.get("/api/export.json")
def export_json():
    conn = db()
    recs = [dict(r) for r in conn.execute("SELECT * FROM records ORDER BY ts DESC").fetchall()]
    cams = [dict(r) for r in conn.execute("SELECT * FROM cameras ORDER BY code").fetchall()]
    conn.close()
    return {"records": recs, "cameras": cams, "exported_at": datetime.now().isoformat()}

# ---------- 静态页面挂载（报价计算器 / 红外相机）----------
# 挂到同一服务 → 页面与API同源 → 一次登录即可全通（无需跨端口认证）
for _name, _dir in [("quote", "/home/ubuntu/www/quote"),
                    ("ircam", "/home/ubuntu/www/ircam")]:
    try:
        app.mount("/" + _name, StaticFiles(directory=_dir, html=True), name=_name)
        print(f"✅ mounted /{_name} → {_dir}")
    except Exception as e:
        print(f"❌ mount /{_name} failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=80)

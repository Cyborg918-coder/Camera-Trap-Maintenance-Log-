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
import secrets
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
AUTH_PASS = os.environ.get("IRCAM_PASS")
# 报价器独立口令：/quote/ 路径使用（可与内部系统口令不同，方便对外分享）
# 不设置时回落到主口令
QUOTE_USER = os.environ.get("QUOTE_USER") or AUTH_USER
QUOTE_PASS = os.environ.get("QUOTE_PASS") or AUTH_PASS
# 管理员口令：修改/删除记录需要（与访问口令分开，避免队员误删）
ADMIN_PASS = os.environ.get("IRCAM_ADMIN_PASS")

# 【2026-09-29 加固】必须通过环境变量注入口令，禁止默认口令生效。
# 原实现有 os.environ.get(..., "CHANGE_ME") 兜底 —— 一旦环境变量丢失，
# 系统会静默地用一个公开已知的口令运行，形同虚设。
# 现在：未配置 → 直接用随机口令（等于无人能访问），并打印醒目警告。
_dev_warn = False
if not AUTH_PASS:
    AUTH_PASS = secrets.token_urlsafe(24)
    _dev_warn = True
if not ADMIN_PASS:
    ADMIN_PASS = secrets.token_urlsafe(24)
    _dev_warn = True
if _dev_warn:
    print("=" * 64)
    print("⚠️  警告：未检测到 IRCAM_PASS / IRCAM_ADMIN_PASS 环境变量！")
    print("    已生成【随机口令】—— 外部将无法登录（这比用默认口令安全）。")
    print("    请检查 systemd 服务配置中的 Environment= 项。")
    print("=" * 64)


@app.middleware("http")
async def no_cache_entry_files(request: Request, call_next):
    """【2026-09-29 补】入口文件禁用 HTTP 缓存。

    为什么必须做：移动端浏览器（尤其 Safari 与各类套壳浏览器）对 sw.js 的缓存极其顽固。
    即使把 sw.js 内部的 CACHE 版本号改成 ircam-v3，只要 HTTP 响应头允许缓存，
    手机可能 24 小时内根本不向服务器发请求比对字节差异
    → 野外下发的紧急修复推送不到队员手机上。

    ⚠️ 为什么用中间件而不是改路由：
      前端注册的是相对路径 register('sw.js')，页面在 /ircam/ 下，
      所以实际请求 /ircam/sw.js —— 由 StaticFiles 挂载处理，
      不会被 @app.get("/sw.js") 那条路由命中。中间件能覆盖所有路径。
    """
    resp = await call_next(request)
    p = request.url.path
    if (p.endswith("/sw.js") or p.endswith("/manifest.json")
            or p.endswith("/index.html") or p.endswith("/ircam/")
            or p == "/ircam" or p == "/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.middleware("http")
async def basic_auth_mw(request: Request, call_next):
    """按路径校验口令：
       /quote/*  → QUOTE_USER / QUOTE_PASS（报价器，可单独设口令对外分享）
       其他路径  → AUTH_USER / AUTH_PASS（内部系统：红外相机 + API）
    """
    path = request.url.path
    # 【2026-09-29】放行 ACME 验证（Let's Encrypt 签发/续期证书用）——
    # 这个路径必须能匿名访问，否则证书无法自动续期
    if path.startswith("/.well-known/acme-challenge/"):
        return await call_next(request)
    if path.startswith("/quote"):
        want_u, want_p, realm = QUOTE_USER, QUOTE_PASS, "Quote"
    else:
        want_u, want_p, realm = AUTH_USER, AUTH_PASS, "IRCAM"

    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            u, p = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
            # 【2026-09-29】用 secrets.compare_digest 做恒定时间比较：
            # 普通 == 会在第一个不同字符处提前返回，理论上可被逐字符试探（时序侧信道）。
            ok_u = secrets.compare_digest(u.encode("utf-8"), want_u.encode("utf-8"))
            ok_p = secrets.compare_digest(p.encode("utf-8"), want_p.encode("utf-8"))
            if ok_u and ok_p:
                return await call_next(request)
        except Exception:
            pass
    return Response("需要登录才能访问", status_code=401,
                    headers={"WWW-Authenticate": f'Basic realm="{realm}"'})


def is_admin(request: Request) -> bool:
    """管理员校验：前端在设置页解锁后，请求头带 x-admin-pass
    【2026-09-29】改用恒定时间比较（secrets.compare_digest），避免时序侧信道。"""
    try:
        return secrets.compare_digest(
            request.headers.get("x-admin-pass", "").encode("utf-8"),
            ADMIN_PASS.encode("utf-8"))
    except Exception:
        return False

def db():
    """【2026-09-29 加固】两个 uvicorn 进程(HTTP:80 / HTTPS:3000)共用同一 SQLite 文件。
    默认 sqlite3.connect() 无超时、用 DELETE journal，并发写入会直接
    抛 sqlite3.OperationalError: database is locked（多人在山上回来同时同步时触发）。
    → timeout=30s + busy_timeout：锁冲突时等待而非立即报错。"""
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")   # 单位：毫秒
    return conn

def init_db():
    conn = db()
    # 【2026-09-29】WAL 模式：读写不互斥，允许"一个进程写、另一个进程读"并发进行
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        print(f"⚠️ 启用 WAL 失败（不影响功能，但并发写性能较差）: {e}")
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

    -- 【2026-09-29】墓碑表：记录"被物理彻底删除(purge)"的记录 ID
    -- 为什么需要：前端靠 /api/deleted-record-ids 清理本地残留记录。
    -- 但 purge 是物理 DELETE，ID 会从 records 表里彻底消失，
    -- 于是那些"在记录被软删除期间一直离线、直到回收站被清空后才上线"的队员手机，
    -- 既不在 deleted_ids 里、也不在 records 列表里 → 永远删不掉这条僵尸记录。
    CREATE TABLE IF NOT EXISTS purged_records (
        id TEXT PRIMARY KEY,
        purged_at TEXT
    );

    CREATE TABLE IF NOT EXISTS operators (
        name TEXT PRIMARY KEY,
        created_at TEXT,
        created_by TEXT
    );

    -- ===== 轨迹库（一条轨迹 = 一条巡查线路，含多台相机）=====
    CREATE TABLE IF NOT EXISTS routes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,            -- 轨迹名称（管理员自定义，如"MTG北线"）
        kml_content TEXT,              -- 最新版 KML
        kml_name TEXT,                 -- KML 文件名
        kml_updated_at TEXT,           -- 最新版更新时间
        kml_updated_by TEXT,           -- 最新版更新人
        note TEXT,                     -- 备注（如"入口在XX桥"）
        created_at TEXT,
        created_by TEXT,
        deleted INTEGER DEFAULT 0,     -- 软删除
        deleted_at TEXT
    );

    -- 轨迹 ↔ 相机 关联（一条相机只属于一条轨迹）
    CREATE TABLE IF NOT EXISTS route_cameras (
        route_id TEXT,
        code TEXT,
        seq INTEGER,                   -- 顺序
        PRIMARY KEY (route_id, code)
    );

    -- KML 历史版本（保留最近3版）
    CREATE TABLE IF NOT EXISTS route_kml_history (
        hid INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id TEXT,
        kml_content TEXT,
        kml_name TEXT,
        saved_at TEXT,
        saved_by TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rk_history ON route_kml_history(route_id, saved_at DESC);
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
# 【2026-09-29 补】入口文件禁用 HTTP 缓存
# 为什么必须：移动端浏览器（尤其 Safari 与各类套壳浏览器）对 sw.js 的缓存极其顽固。
# 即使把 sw.js 内部的 CACHE 版本号改成 ircam-v3，只要 HTTP 响应头允许缓存，
# 手机可能 24 小时内根本不向服务器发请求比对字节差异
# → 野外下发的紧急修复推送不到队员手机上。
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.get("/")
def index():
    return FileResponse(BASE / "index.html", headers=NO_CACHE_HEADERS)

@app.get("/sw.js")
def sw():
    return FileResponse(BASE / "sw.js", media_type="application/javascript",
                        headers=NO_CACHE_HEADERS)

@app.get("/manifest.json")
def manifest():
    return FileResponse(BASE / "manifest.json", media_type="application/manifest+json",
                        headers=NO_CACHE_HEADERS)

# ---------- API ----------
@app.post("/api/records")
async def post_records(req: Request):
    """接收维护记录(支持批量)"""
    data = await req.json()
    recs = data.get("records", [])
    conn = db()
    admin = is_admin(req)          # 【安全 2026-09-29】见下方已存在记录的处理
    accepted = []
    skipped = []
    for r in recs:
        try:
            rid = r.get("id")
            # 【安全 2026-09-29】已存在的记录：仅管理员可覆盖。
            # 背景：原来用 INSERT OR REPLACE，队员只要构造一个已知 ID 的 payload
            #       就能绕过 PATCH /api/record/{rid} 的管理员校验，静默改写他人历史记录。
            # 现在：非管理员提交"已存在 ID" → 跳过并记录，不写入。
            if rid and conn.execute("SELECT id FROM records WHERE id=?", (rid,)).fetchone():
                if not admin:
                    skipped.append(rid)
                    continue
            conn.execute(
                """INSERT OR REPLACE INTO records
                   (id, ts, operator, code, ops, lat, lng, note, photo, kml_name, kml_content, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, r.get("ts"), r.get("operator"), r.get("code"),
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
    out = {"ok": True, "accepted": accepted, "count": len(accepted)}
    if skipped:
        out["skipped"] = skipped
        out["skipped_count"] = len(skipped)
        print(f"⚠️ 非管理员尝试覆盖已存在记录，已拒绝 {len(skipped)} 条")
    return out

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


@app.get("/api/deleted-record-ids")
def get_deleted_record_ids():
    """【2026-09-29】返回【已被软删除】的记录 ID 列表。

    为什么需要：前端做"服务器为准"的本地清理时，原来靠比对
    GET /api/records 返回的列表做差集 —— 但那个接口有 limit（前端传 2000）。
    记录一旦超过 2000 条，早期的有效记录就不在返回列表里，
    会被前端误判成"管理员已删除"而从队员手机上抹掉。

    现在前端改为依据这个【明确名单】删除，不再靠差集推断。
    """
    conn = db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS purged_records (id TEXT PRIMARY KEY, purged_at TEXT)")
    except Exception:
        pass
    # 软删除的 + 已被物理彻底删除的（墓碑），并集返回
    rows = conn.execute("""
        SELECT id FROM records WHERE COALESCE(deleted,0)=1
        UNION
        SELECT id FROM purged_records
    """).fetchall()
    conn.close()
    return {"ok": True, "deleted_ids": [r[0] for r in rows], "count": len(rows)}


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
    # 【2026-09-29】先写"墓碑"，再物理删除。
    # 墓碑保证：即便这行已从 records 表消失，长期离线的队员手机上线后
    # 仍能从 /api/deleted-record-ids 知道"这条已被删除"，从而清掉本地残留。
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS purged_records (id TEXT PRIMARY KEY, purged_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO purged_records VALUES (?, ?)",
                     (rid, datetime.now().isoformat()))
    except Exception as e:
        print(f"⚠️ 写墓碑失败（不影响删除本身）: {e}")
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

# ==================== 轨迹库（巡查线路）====================
# 设计：一条轨迹 = 一条巡查线路，含 KML + 关联的多台相机
# 权限：查看/新增/改KML/关联相机 = 所有队员；删除 = 软删除（可恢复）
# KML 历史：每次更新把旧版存入 route_kml_history，只保留最近 3 版

KML_KEEP_VERSIONS = 3

def _route_brief(conn, r):
    cams = [x[0] for x in conn.execute(
        "SELECT code FROM route_cameras WHERE route_id=? ORDER BY seq, code", (r["id"],)).fetchall()]
    return {
        "id": r["id"], "name": r["name"], "note": r["note"],
        "cameras": cams, "camera_count": len(cams),
        "kml_name": r["kml_name"],
        "has_kml": bool(r["kml_content"]),
        "kml_size": len(r["kml_content"]) if r["kml_content"] else 0,
        "kml_updated_at": r["kml_updated_at"], "kml_updated_by": r["kml_updated_by"],
        "created_at": r["created_at"], "created_by": r["created_by"],
    }


@app.get("/api/routes")
def list_routes():
    """轨迹列表（所有队员可读）"""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM routes WHERE COALESCE(deleted,0)=0 ORDER BY name").fetchall()
    out = [_route_brief(conn, r) for r in rows]
    conn.close()
    return {"ok": True, "routes": out, "count": len(out)}


@app.get("/api/routes/{rid}")
def get_route(rid: str):
    """单条轨迹详情（不含 KML 内容，避免大流量）"""
    conn = db()
    r = conn.execute("SELECT * FROM routes WHERE id=? AND COALESCE(deleted,0)=0", (rid,)).fetchone()
    if not r:
        conn.close()
        return {"ok": False, "error": "轨迹不存在"}
    d = _route_brief(conn, r)
    conn.close()
    return {"ok": True, "route": d}


@app.post("/api/routes")
def create_route(payload: dict):
    """新建轨迹（所有队员可建）"""
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "轨迹名称不能为空"}
    rid = "RT" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]
    conn = db()
    dup = conn.execute("SELECT id FROM routes WHERE name=? AND COALESCE(deleted,0)=0", (name,)).fetchone()
    if dup:
        conn.close()
        return {"ok": False, "error": f"轨迹「{name}」已存在"}
    conn.execute(
        "INSERT INTO routes (id, name, note, created_at, created_by) VALUES (?,?,?,?,?)",
        (rid, name, (payload.get("note") or "").strip(), datetime.now().isoformat(),
         payload.get("by") or ""))
    conn.commit()
    conn.close()
    return {"ok": True, "id": rid, "name": name}


@app.patch("/api/routes/{rid}")
def update_route(rid: str, request: Request, payload: dict):
    """改轨迹名称/备注 —— 仅管理员"""
    if not is_admin(request):
        return {"ok": False, "need_admin": True, "error": "改名称/备注需要管理员权限"}
    conn = db()
    fields, args = [], []
    if "name" in payload:
        nm = (payload["name"] or "").strip()
        if not nm:
            conn.close()
            return {"ok": False, "error": "名称不能为空"}
        dup = conn.execute("SELECT id FROM routes WHERE name=? AND id<>? AND COALESCE(deleted,0)=0",
                           (nm, rid)).fetchone()
        if dup:
            conn.close()
            return {"ok": False, "error": f"轨迹「{nm}」已存在"}
        fields.append("name=?"); args.append(nm)
    if "note" in payload:
        fields.append("note=?"); args.append((payload["note"] or "").strip())
    if not fields:
        conn.close()
        return {"ok": False, "error": "无可更新字段"}
    args.append(rid)
    conn.execute(f"UPDATE routes SET {', '.join(fields)} WHERE id=?", args)
    conn.commit()
    conn.close()
    return {"ok": True, "id": rid}


@app.delete("/api/routes/{rid}")
def delete_route(rid: str, request: Request):
    """删除轨迹（软删除，可恢复）—— 仅管理员"""
    if not is_admin(request):
        return {"ok": False, "need_admin": True, "error": "删除轨迹需要管理员权限"}
    conn = db()
    cur = conn.execute(
        "UPDATE routes SET deleted=1, deleted_at=? WHERE id=? AND COALESCE(deleted,0)=0",
        (datetime.now().isoformat(), rid))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "deleted": rid}


def _kml_headers(fname: str):
    """KML 下载响应头：中文文件名用 RFC 5987 编码，并给 ASCII 兜底"""
    from urllib.parse import quote
    safe = (fname or "route.kml").replace('"', '').replace('\n', '')
    ascii_fb = "route.kml"
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_fb}\"; filename*=UTF-8''{quote(safe)}",
        "Cache-Control": "no-store",
    }


@app.get("/api/routes/{rid}/kml")
def download_route_kml(rid: str):
    """下载轨迹最新版 KML"""
    conn = db()
    r = conn.execute("SELECT name, kml_content, kml_name FROM routes WHERE id=? AND COALESCE(deleted,0)=0",
                     (rid,)).fetchone()
    conn.close()
    if not r or not r["kml_content"]:
        return {"ok": False, "error": "该轨迹还没有 KML 文件"}
    fname = r["kml_name"] or ((r["name"] or "route") + ".kml")
    return Response(content=r["kml_content"],
                    media_type="application/vnd.google-earth.kml+xml",
                    headers=_kml_headers(fname))


@app.post("/api/routes/{rid}/kml")
def update_route_kml(rid: str, payload: dict):
    """更新轨迹 KML（旧版存入历史，只保留最近3版）"""
    content = payload.get("kml_content") or ""
    if not content.strip():
        return {"ok": False, "error": "KML 内容为空"}
    conn = db()
    r = conn.execute("SELECT * FROM routes WHERE id=? AND COALESCE(deleted,0)=0", (rid,)).fetchone()
    if not r:
        conn.close()
        return {"ok": False, "error": "轨迹不存在"}

    # 旧版入历史
    if r["kml_content"]:
        conn.execute(
            "INSERT INTO route_kml_history (route_id, kml_content, kml_name, saved_at, saved_by) VALUES (?,?,?,?,?)",
            (rid, r["kml_content"], r["kml_name"], r["kml_updated_at"] or r["created_at"],
             r["kml_updated_by"] or r["created_by"]))
        # 只保留最近 KML_KEEP_VERSIONS 版
        conn.execute(
            "DELETE FROM route_kml_history WHERE route_id=? AND hid NOT IN "
            "(SELECT hid FROM route_kml_history WHERE route_id=? ORDER BY saved_at DESC LIMIT ?)",
            (rid, rid, KML_KEEP_VERSIONS))

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE routes SET kml_content=?, kml_name=?, kml_updated_at=?, kml_updated_by=? WHERE id=?",
        (content, payload.get("kml_name") or "route.kml", now, payload.get("by") or "", rid))
    conn.commit()
    hist_n = conn.execute("SELECT COUNT(*) FROM route_kml_history WHERE route_id=?", (rid,)).fetchone()[0]
    conn.close()
    return {"ok": True, "id": rid, "updated_at": now, "history_versions": hist_n}


@app.get("/api/routes/{rid}/history")
def route_kml_history(rid: str):
    """KML 历史版本列表"""
    conn = db()
    rows = conn.execute(
        "SELECT hid, kml_name, saved_at, saved_by, LENGTH(kml_content) AS size "
        "FROM route_kml_history WHERE route_id=? ORDER BY saved_at DESC", (rid,)).fetchall()
    conn.close()
    return {"ok": True, "versions": [dict(r) for r in rows], "count": len(rows)}


@app.get("/api/routes/{rid}/history/{hid}")
def download_history_kml(rid: str, hid: int):
    """下载某个历史版本"""
    conn = db()
    r = conn.execute("SELECT kml_name, kml_content FROM route_kml_history WHERE route_id=? AND hid=?",
                     (rid, hid)).fetchone()
    conn.close()
    if not r:
        return {"ok": False, "error": "历史版本不存在"}
    return Response(content=r["kml_content"],
                    media_type="application/vnd.google-earth.kml+xml",
                    headers=_kml_headers(r["kml_name"] or f"history_{hid}.kml"))


@app.post("/api/routes/{rid}/restore/{hid}")
def restore_route_kml(rid: str, hid: int, payload: dict = None):
    """把某个历史版本恢复为最新版（当前版会再入历史）"""
    conn = db()
    h = conn.execute("SELECT kml_content, kml_name FROM route_kml_history WHERE route_id=? AND hid=?",
                     (rid, hid)).fetchone()
    cur = conn.execute("SELECT * FROM routes WHERE id=? AND COALESCE(deleted,0)=0", (rid,)).fetchone()
    if not h or not cur:
        conn.close()
        return {"ok": False, "error": "轨迹或历史版本不存在"}
    if cur["kml_content"]:
        conn.execute(
            "INSERT INTO route_kml_history (route_id, kml_content, kml_name, saved_at, saved_by) VALUES (?,?,?,?,?)",
            (rid, cur["kml_content"], cur["kml_name"], cur["kml_updated_at"] or cur["created_at"],
             cur["kml_updated_by"] or cur["created_by"]))
        conn.execute(
            "DELETE FROM route_kml_history WHERE route_id=? AND hid NOT IN "
            "(SELECT hid FROM route_kml_history WHERE route_id=? ORDER BY saved_at DESC LIMIT ?)",
            (rid, rid, KML_KEEP_VERSIONS))
    now = datetime.now().isoformat()
    conn.execute("UPDATE routes SET kml_content=?, kml_name=?, kml_updated_at=?, kml_updated_by=? WHERE id=?",
                 (h["kml_content"], h["kml_name"], now, "恢复历史版本", rid))
    conn.commit()
    conn.close()
    return {"ok": True, "restored": hid}


@app.put("/api/routes/{rid}/cameras")
def set_route_cameras(rid: str, payload: dict):
    """设置轨迹关联的相机（全量替换）"""
    codes = payload.get("codes") or []
    if not isinstance(codes, list):
        return {"ok": False, "error": "codes 必须是数组"}
    conn = db()
    r = conn.execute("SELECT id FROM routes WHERE id=? AND COALESCE(deleted,0)=0", (rid,)).fetchone()
    if not r:
        conn.close()
        return {"ok": False, "error": "轨迹不存在"}
    conn.execute("DELETE FROM route_cameras WHERE route_id=?", (rid,))
    for i, c in enumerate(codes):
        cc = str(c).strip().upper()
        if cc:
            conn.execute("INSERT OR REPLACE INTO route_cameras (route_id, code, seq) VALUES (?,?,?)",
                         (rid, cc, i))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM route_cameras WHERE route_id=?", (rid,)).fetchone()[0]
    conn.close()
    return {"ok": True, "route_id": rid, "camera_count": n}


@app.get("/api/camera/{code}/route")
def camera_route(code: str):
    """查询某台相机属于哪条轨迹（供台账页显示）"""
    conn = db()
    rows = conn.execute(
        "SELECT r.id, r.name FROM route_cameras rc JOIN routes r ON r.id = rc.route_id "
        "WHERE rc.code=? AND COALESCE(r.deleted,0)=0", (code.upper(),)).fetchall()
    conn.close()
    return {"ok": True, "code": code.upper(), "routes": [{"id": x[0], "name": x[1]} for x in rows]}


@app.get("/api/routes-map")
def routes_map():
    """相机 → 轨迹 的映射表（前端台账页一次性拉取）"""
    conn = db()
    rows = conn.execute(
        "SELECT rc.code, r.id, r.name FROM route_cameras rc JOIN routes r ON r.id = rc.route_id "
        "WHERE COALESCE(r.deleted,0)=0").fetchall()
    conn.close()
    m = {}
    for code, rid, name in rows:
        m.setdefault(code, []).append({"id": rid, "name": name})
    return {"ok": True, "map": m}


# ==================== 维护人名单 ====================
@app.get("/api/operators")
def list_operators():
    """维护人名单（所有队员都能读，用于录入页下拉选择）"""
    conn = db()
    rows = [r[0] for r in conn.execute("SELECT name FROM operators ORDER BY name").fetchall()]
    conn.close()
    return {"ok": True, "operators": rows, "count": len(rows)}


@app.post("/api/operators")
def add_operator(payload: dict, request: Request):
    """添加维护人（需要管理员口令）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "姓名不能为空"}
    conn = db()
    cur = conn.execute(
        "INSERT INTO operators (name, created_at) VALUES (?,?) ON CONFLICT(name) DO NOTHING",
        (name, datetime.now().isoformat()))
    conn.commit()
    added = cur.rowcount
    conn.close()
    return {"ok": True, "name": name, "added": added > 0}


@app.delete("/api/operators/{name}")
def del_operator(name: str, request: Request):
    """删除维护人（需要管理员口令；已保存的历史记录不受影响）"""
    if not is_admin(request):
        return {"ok": False, "error": "需要管理员口令", "need_admin": True}
    conn = db()
    cur = conn.execute("DELETE FROM operators WHERE name=?", (name,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": n > 0, "name": name}


@app.get("/api/cameras")
def get_cameras():
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cameras ORDER BY code").fetchall()]
    conn.close()
    return {"cameras": rows, "count": len(rows)}

@app.post("/api/cameras")
async def post_cameras(req: Request):
    """批量导入/更新相机台账 —— 仅管理员
    【安全 2026-09-29】原实现漏了管理员校验（同功能的 /api/cameras/bulk 反而有校验）。
    未校验时，队员可 POST 本接口直接改写全山相机的坐标/状态（含把相机设为已移除）。
    """
    if not is_admin(req):
        return {"ok": False, "need_admin": True, "error": "批量更新相机台账需要管理员权限"}
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
def export_json(include_photos: bool = False):
    """全量导出（JSON 备份）

    【2026-09-29 加固】默认【不含】照片 base64 与 KML 正文。
    原来用 SELECT * 会把所有历史照片一次性读进内存（单条 150~250KB），
    记录积累到数千条时轻量服务器（1~2GB 内存）会被 OOM Killer 杀掉进程。
    需要含照片的完整备份时显式加 ?include_photos=1（请确认内存充足）。
    """
    conn = db()
    if include_photos:
        cols = "*"
        note = "含照片/KML正文（文件较大）"
    else:
        cols = ("id, ts, operator, code, ops, lat, lng, note, kml_name, created_at, "
                "deleted, deleted_at, deleted_by")
        note = "不含照片/KML正文；需完整备份请加 ?include_photos=1"
    recs = [dict(r) for r in conn.execute(f"SELECT {cols} FROM records ORDER BY ts DESC").fetchall()]
    cams = [dict(r) for r in conn.execute("SELECT * FROM cameras ORDER BY code").fetchall()]
    conn.close()
    return {"records": recs, "cameras": cams,
            "exported_at": datetime.now().isoformat(),
            "includes_photos": bool(include_photos),
            "note": note}

# ---------- Let's Encrypt 证书验证（ACME HTTP-01）----------
# 【2026-09-29】为 iOS 队员引入"受系统信任"的证书（nip.io 域名 + Let's Encrypt）。
# iOS 的 PWA（添加到主屏）不继承 Safari 里手动信任的自签证书 → 白屏。
# certbot --webroot 会把验证文件写到下面这个目录，我们把它暴露出来。
ACME_DIR = Path("/var/www/certbot/.well-known/acme-challenge")

@app.get("/.well-known/acme-challenge/{token}")
def acme_challenge(token: str):
    # 防路径穿越：token 只保留安全字符
    safe = "".join(c for c in token if c.isalnum() or c in "-_")
    f = ACME_DIR / safe
    if f.is_file():
        return PlainTextResponse(f.read_text())
    return PlainTextResponse("not found", status_code=404)

# ---------- 静态页面挂载（报价计算器 / 红外相机）----------
# 挂到同一服务 → 页面与API同源 → 一次登录即可全通（无需跨端口认证）
for _name, _dir in [("quote", "/home/ubuntu/www/quote"),
                    ("ircam", "/home/ubuntu/www/ircam")]:
    try:
        # follow_symlink=True：允许 www/<name>/ 下的文件是指向源目录的软链接
        # 用途：www/ircam/index.html → /home/ubuntu/ircam/index.html
        #       这样改源文件立即生效，不再需要手动 cp（2026-09-29 教训）
        # 安全性：该目录内只有静态文件（html/js/json/cert），不暴露 db/venv/私钥
        app.mount("/" + _name, StaticFiles(directory=_dir, html=True, follow_symlink=True), name=_name)
        print(f"✅ mounted /{_name} → {_dir}")
    except Exception as e:
        print(f"❌ mount /{_name} failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=80)

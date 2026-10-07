# 红外相机维护日志系统

[English](README.md) · **简体中文**

给野外红外相机监测（camera trap）用的**离线优先**维护日志工具。野外无信号也能记录，
回到有网自动汇总全队数据。

> 为北京山区一个真实的红外相机调查项目而做：上百台相机、多名野外队员、调查点完全没有手机信号。

---

## 为什么做这个

红外相机调查的日常维护有几个共性痛点：

- 野外**没有手机信号**，在线表格和表单根本打不开
- **纸质记录**容易丢失、遗漏、字迹潦草
- 维护**进度不透明**——不知道哪台相机该去了、谁最后去过
- 拍的照片和坐标**对不上号**，回来整理全靠回忆

这套系统把"维护日志"装进手机：离线可用 + 拍照定位 + 自动汇总 + 台账透明。

---

## 功能

### 离线优先
- PWA + Service Worker 离线缓存——出发前联网打开一次，之后没信号也能用
- 数据先写入浏览器本地（`localStorage`），一有网络自动上传

### 维护记录
- 相机编号 + 维护人 + 操作类型（可多选）+ 自由备注
- 自动获取 GPS 坐标（需安全上下文，见[为什么必须用 HTTPS](#为什么必须用-https)）
- 现场照片，存入前用 `canvas` 自动压缩
- KML 轨迹文件上传（两步路等 App 导出）

### 多人协作
- FastAPI + SQLite 后端汇总所有人的记录
- **自动双向同步**——上传本机未同步的记录，同时拉取服务器上其他人的记录
- 相机台账自动维护：哪台相机最后被谁、什么时候维护过

### 权限管理
- **访问口令**（HTTP Basic Auth）——队员用，能看能录
- **管理员口令**——修改/删除需单独校验，队员看不到相关按钮
- **回收站**（软删除）——误删可恢复，彻底删除需二次确认

### 轨迹管理
- 命名的调查路线（如「MTG北线」），可为每条路线指定相机顺序
- 每条路线一份 KML，带**历史版本**——保留最近 3 版，任意版本可恢复

### 省流量设计
- 照片**按需加载**：列表接口只返回文字字段，点开某条记录才下载它的照片
- `GET /api/record/{rid}` 返回单条完整记录（含 base64 照片）

---

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 单文件 HTML + 原生 JS——无框架、无构建步骤 |
| 后端 | FastAPI + SQLite（WAL）+ uvicorn |
| 部署 | systemd + 自签证书 TLS |
| 离线 | Service Worker + Cache API + localStorage |

没有构建工具、没有打包器、没有 npm。改完 `index.html` 刷新即可。

---

## 快速开始

### 后端

```bash
python -m venv venv
venv/bin/pip install fastapi uvicorn

# 口令只从环境变量读取，代码内不写死
export IRCAM_USER=team
export IRCAM_PASS=your_password
export IRCAM_ADMIN_PASS=your_admin_password

venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

数据库（`ircam.db`）与表结构在首次启动时自动创建。

### 前端

`index.html` 是单文件应用——放到任意静态目录，或直接由后端提供。

前后端同源部署时无需任何配置：前端的 `DEFAULT_SERVER = ''` 就表示同源。

---

## 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `IRCAM_USER` | 访问用户名 | `team` |
| `IRCAM_PASS` | 访问口令（队员用）| **不设置** ⚠️ |
| `IRCAM_ADMIN_PASS` | 管理员口令（改/删数据用）| **不设置** ⚠️ |
| `QUOTE_USER` | 仅 `/quote/*` 路径使用的用户名 | 回落到 `IRCAM_USER` |
| `QUOTE_PASS` | 仅 `/quote/*` 路径使用的口令 | 回落到 `IRCAM_PASS` |

**安全行为**：如果没设置 `IRCAM_PASS`，服务器会在启动时**生成一个随机口令并打印出来**，
而不是回落到一个公开已知的默认值。系统默认是**关闭**的，不会默认敞开——
不存在"忘了改默认口令"这种事故。

`QUOTE_USER` / `QUOTE_PASS` 的用途：让一个可对外公开的页面挂在同一个服务器下，
但用一套可以单独分享的口令。

### 生产部署（systemd 示例）

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
# 仅当需要绑定 80 等特权端口时：
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ExecStart=/path/to/venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

HTTPS 版本在 `ExecStart` 末尾追加：

```
--ssl-keyfile /path/to/key.pem --ssl-certfile /path/to/cert.pem
```

---

## 为什么必须用 HTTPS

以下两项功能要求浏览器处于**安全上下文**（secure context），在纯 HTTP 下会被静默拒绝：

- **Service Worker**（离线缓存）→ 野外打不开页面
- **Geolocation**（GPS 定位）→ 定位失败

没有域名时，自签证书是唯一可行方案：

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=YOUR_IP" \
  -addext "subjectAltName=IP:YOUR_IP"
```

然后每位队员在手机上手动信任一次该证书。Android 与 iOS 步骤不同——
**iOS 需额外到「设置 → 通用 → 关于本机 → 证书信任设置」里手动开启信任**。

---

## API 一览

所有 `/api/*` 接口都需要访问口令。标注 **管理员** 的接口还需要在请求头
`x-admin-pass` 中提供管理员口令。

### 记录

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/records` | 记录列表（仅文字字段，带 `has_photo` 标记）| 访问 |
| `POST` | `/api/records` | 上传记录（支持批量）| 访问 |
| `GET` | `/api/record/{rid}` | 单条完整记录（含照片 / KML）| 访问 |
| `PATCH` | `/api/record/{rid}` | 修改记录 | 管理员 |
| `DELETE` | `/api/record/{rid}` | 软删除 → 移入回收站 | 管理员 |
| `GET` | `/api/deleted-record-ids` | 已被软删除的记录 ID（供前端同步用）| 访问 |

### 回收站

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/trash` | 回收站列表 | 管理员 |
| `POST` | `/api/restore/{rid}` | 从回收站恢复 | 管理员 |
| `DELETE` | `/api/purge/{rid}` | 彻底删除（不可恢复）| 管理员 |

### 相机

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/cameras` | 相机台账 | 访问 |
| `POST` | `/api/cameras` | 批量覆盖式导入（含坐标 / 状态），已存在的会被更新 | 管理员 |
| `POST` | `/api/camera` | 新增 / 更新一台相机 | 管理员 |
| `POST` | `/api/cameras/bulk` | 批量导入相机编号（已存在的跳过，不改动）| 管理员 |
| `DELETE` | `/api/camera/{code}` | 删除相机（其历史记录保留）| 管理员 |
| `GET` | `/api/camera/{code}/route` | 查询某台相机属于哪条轨迹 | 访问 |

### 轨迹

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/routes` | 轨迹列表 | 访问 |
| `POST` | `/api/routes` | 新建轨迹 | 访问 |
| `GET` | `/api/routes/{rid}` | 轨迹详情（不含 KML 正文）| 访问 |
| `PATCH` | `/api/routes/{rid}` | 改名称 / 备注 | 管理员 |
| `DELETE` | `/api/routes/{rid}` | 软删除轨迹 | 管理员 |
| `GET` | `/api/routes-map` | 相机 → 轨迹 映射表（台账页一次性拉取）| 访问 |
| `GET` | `/api/routes/{rid}/kml` | 下载当前 KML | 访问 |
| `POST` | `/api/routes/{rid}/kml` | 更新 KML（旧版自动存入历史）| 访问 |
| `GET` | `/api/routes/{rid}/history` | KML 历史版本列表 | 访问 |
| `GET` | `/api/routes/{rid}/history/{hid}` | 下载某个历史版本 | 访问 |
| `POST` | `/api/routes/{rid}/restore/{hid}` | 把历史版本恢复为当前版 | 访问 |

### 维护人

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/operators` | 维护人名单（用于录入页下拉选择）| 访问 |
| `POST` | `/api/operators` | 添加维护人 | 管理员 |
| `DELETE` | `/api/operators/{name}` | 删除维护人（已保存的历史记录不受影响）| 管理员 |

### 统计与导出

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| `GET` | `/api/stats` | 统计信息 | 访问 |
| `GET` | `/api/export.csv` | 导出维护记录 CSV | 访问 |
| `GET` | `/api/export.json` | 全量导出 JSON（备份用）| 访问 |

### 静态资源

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 单文件前端 |
| `GET` | `/sw.js` | Service Worker |
| `GET` | `/manifest.json` | PWA 清单 |

---

## 数据库结构

```sql
records (
  id TEXT PRIMARY KEY,
  ts TEXT, operator TEXT, code TEXT,
  ops TEXT,              -- 操作类型的 JSON 数组
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

首次启动自动建表。已有的库会**原地迁移**——`init_db()` 会为 `records` 表补上缺失的
软删除字段（`deleted`、`deleted_at`、`deleted_by`）。

---

## 项目结构

```
ircam/
├── index.html      # 单文件前端（录入 / 记录 / 台账 / 设置）
├── server.py       # FastAPI 后端 + SQLite
├── sw.js           # Service Worker（离线缓存）
├── manifest.json   # PWA 清单
├── LICENSE         # MIT
├── .gitignore
└── README.md
```

> 运行期产生的 `venv/`、`ircam.db`、`cert/`，以及本站专用的部署脚本，
> **都已在 `.gitignore` 中排除**。现场调查数据、证书与部署凭据绝不入库。

---

## 给贡献者的说明

- **现场数据与凭据绝不进入仓库。** `.gitignore` 负责这件事——请不要依赖"记得别提交"。
- 口令只从环境变量读取。**不要**加硬编码的兜底默认值——用已知口令启动比启动失败更糟。
- `index.html` 是刻意做成零依赖的，请不要引入构建步骤。

---

## 许可

[MIT](LICENSE)

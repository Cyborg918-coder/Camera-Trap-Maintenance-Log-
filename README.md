# 红外相机维护日志系统

给野外红外相机监测用的**离线优先**维护日志工具。野外无信号也能记录，回到有网自动汇总全队数据。

---

## 为什么做这个

红外相机监测（野生动物调查）的日常维护有个共性痛点：

- 野外**没有手机信号**，在线表格打不开
- **纸质记录**容易丢失、遗漏、字迹潦草
- 维护**进度不透明**——不知道哪台相机该去了、谁去过
- 拍的照片和现场位置**对不上号**

这套系统把"维护日志"装进手机，做到离线可用 + 拍照定位 + 自动汇总 + 台账透明。

---

## 功能

### 离线优先
- PWA + Service Worker 离线缓存（出发前联网打开一次，之后无信号可用）
- 数据先存浏览器本地（localStorage），有网络时自动上传

### 维护记录
- 选择相机编号 + 维护人 + 操作类型（可多选）
- 自动获取 GPS 坐标（HTTPS 下）
- 现场照片（canvas 自动压缩后存储）
- KML 轨迹文件上传（两步路等 App 导出）
- 自由备注

### 多人协作
- 后端 FastAPI + SQLite 汇总
- **自动双向同步**：上传本机未同步记录 + 拉取服务器上其他人的记录
- 相机台账自动更新（哪台最后被谁维护过）

### 权限管理
- 访问口令（HTTP Basic Auth）——队员用，能看能录
- **管理员口令**——修改/删除需单独解锁，队员看不到相关按钮
- **回收站**（软删除）——误删可恢复，彻底删除需二次确认

### 省流量设计
- 照片**按需加载**：列表只拉文字字段，点开某张照片才下载该条记录
- 单条记录接口 `/api/record/{id}` 返回完整内容（含照片 base64）

---

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 单文件 HTML + 原生 JS（无框架、无构建步骤）|
| 后端 | FastAPI + SQLite + uvicorn |
| 部署 | systemd + 自签证书 HTTPS |
| 离线 | Service Worker + Cache API |

---

## 快速开始

### 后端

```bash
python -m venv venv
venv/bin/pip install fastapi uvicorn

# 口令必须通过环境变量提供
export IRCAM_USER=team
export IRCAM_PASS=your_password
export IRCAM_ADMIN_PASS=your_admin_password

venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

### 前端

`index.html` 是单文件应用，放到任意静态目录即可。
前后端同源部署时无需配置服务器地址（前端 `DEFAULT_SERVER = ''` 表示同源）。

---

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `IRCAM_USER` | 访问用户名 | `team` |
| `IRCAM_PASS` | 访问口令（队员用）| `CHANGE_ME` ⚠️ 必须修改 |
| `IRCAM_ADMIN_PASS` | 管理员口令（改删数据用）| `CHANGE_ME_ADMIN` ⚠️ 必须修改 |

---

## 生产部署（systemd 示例）

```ini
[Unit]
Description=IRCAM Maintenance System
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ircam
Environment=IRCAM_USER=team
Environment=IRCAM_PASS=your_password
Environment=IRCAM_ADMIN_PASS=your_admin_password
# 如需绑定 80 等特权端口：
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

## API 一览

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/records` | 记录列表（不含照片内容，带 has_photo 标记）| 访问口令 |
| GET | `/api/record/{id}` | 单条记录完整内容（含照片/KML）| 访问口令 |
| POST | `/api/records` | 批量上传记录 | 访问口令 |
| PATCH | `/api/record/{id}` | 修改记录 | 管理员 |
| DELETE | `/api/record/{id}` | 删除记录（移入回收站）| 管理员 |
| GET | `/api/trash` | 回收站列表 | 管理员 |
| POST | `/api/restore/{id}` | 从回收站恢复 | 管理员 |
| DELETE | `/api/purge/{id}` | 彻底删除 | 管理员 |
| GET | `/api/cameras` | 相机台账 | 访问口令 |
| POST | `/api/camera` | 新增/更新相机 | 管理员 |
| POST | `/api/cameras/bulk` | 批量导入相机编号 | 管理员 |
| DELETE | `/api/camera/{code}` | 删除相机 | 管理员 |
| GET | `/api/stats` | 统计信息 | 访问口令 |
| GET | `/api/export.csv` | 导出 CSV | 访问口令 |
| GET | `/api/export.json` | 导出 JSON | 访问口令 |

管理员接口需要请求头 `x-admin-pass: <管理员口令>`。

---

## 数据库结构

**records**

```
id, ts, operator, code, ops(JSON数组), lat, lng, note,
photo(base64), kml_name, kml_content, created_at,
deleted, deleted_at, deleted_by
```

**cameras**

```
code, status(active/removed), lat, lng,
last_maint, last_by, updated_at
```

首次启动自动建表；已有库会自动补 `deleted` 等新增字段（迁移逻辑在 `init_db()`）。

---

## HTTPS 的必要性

以下两项功能要求"安全上下文"（HTTPS），HTTP 下浏览器会拒绝：

- **Service Worker**（离线缓存）→ 野外打不开页面
- **Geolocation**（GPS 定位）→ 定位失败

自签证书方案（无域名时唯一可行）：

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=YOUR_IP" \
  -addext "subjectAltName=IP:YOUR_IP"
```

然后每位队员在手机上手动信任一次该证书（Android / iOS 步骤不同，iOS 需额外开启"证书信任设置"）。

---

## 项目结构

```
ircam/
├── index.html      # 单文件前端（录入/记录/台账/设置）
├── server.py       # FastAPI 后端 + SQLite
├── sw.js           # Service Worker（离线缓存）
├── manifest.json   # PWA 清单
├── LICENSE         # MIT
├── .gitignore
└── README.md
```

> 运行期产生的 `venv/`、`ircam.db`、`cert/` 与本站部署脚本**已在 `.gitignore` 中排除**。
> 数据库与证书绝不入库。

---

## 许可

MIT License

# GEO 引擎：单用户本地版 → 多用户服务器端部署 · 评估与实施方案

> 适用范围：当前 `geo_engine`（纯标准库、单 SQLite、磁盘共享业务线）改造为「部署在服务器、支持多用户并发、数据隔离」的 SaaS/内网服务。
> 原则：**核心引擎零改动复用**，只在其外包裹「租户命名空间 + Web/API/鉴权/任务/部署」层。

---

## 0. 一句话结论

当前代码是**进程内、单库、全局共享配置**的设计，直接加 Web 层会让多个用户读到彼此的业务线、互相踩锁、长任务阻塞请求。改造的关键是：

1. 引入 **tenant（租户）** 作为一切数据与配置的最外层命名空间；
2. 把同步长任务 `GeoPipeline.run()` 包成**后台作业**；
3. 加 **JWT 鉴权 + 请求级租户依赖**，从入口强制数据隔离；
4. 把 SQLite 单连接改成**每租户库文件 / 连接池 + WAL**，或用 PostgreSQL；
5. 用 **Docker + nginx + HTTPS** 部署到一台服务器。

预计工作量：MVP（单库每租户 + FastAPI + JWT + Docker）约 **8–12 人日**；完整版（PostgreSQL + 任务队列 + 计量/管理后台）约 **18–25 人日**。

---

## 1. 现状评估（基于真实代码）

| 维度 | 现状 | 多用户下的风险 |
|------|------|----------------|
| 存储 `store.py` | 单文件 `data/geo.db`，全局单连接 `sqlite3.connect(..., check_same_thread=False)` + `threading.RLock`；表仅有 `business_line` 列 | 多 worker 进程各自开连接 → 写竞争 `database is locked`；**无任何 `user_id/tenant_id`**，用户间数据不隔离 |
| 配置 `config.py` | `business_lines/*.json` 磁盘全局文件，`ConfigRepository` 扫目录共享；`Settings` 按 `root` 路径 | 所有用户看到同一批业务线，无法私有化 |
| 产物磁盘 `dist/ reports/ content/` | 按 `<bl_id>` 平铺在共享根目录 | 用户 A 能读到用户 B 的 llms.txt / 监测报表 |
| 编排 `pipeline.py` | `GeoPipeline.run()` 同步、进程内、可能长耗时（LLM/建站写盘），无任务 ID/进度 | 并发请求互相阻塞；无法查询「我的任务跑完了没」 |
| 注册表 `registry.py` | 进程内 `REGISTRY` 字典，import 时注册 | 多进程下每个 worker 各自一份（可接受，因注册在 import 期完成） |
| 核心引擎 `ingest/structure/semantic/formats/distribute/monitor` | 纯函数/类，无全局可变状态，数据靠参数与 SQLite 传递 | ✅ **无需改动**，天然可复用 |
| 鉴权/会话 | 无 | 必须从零引入 |

**结论**：风险集中在「存储隔离、配置共享、任务同步、鉴权缺失」四点；核心算法层健康，改造是「加壳」而非「重写」。

---

## 2. 目标架构

```
                        ┌──────────────────────────────────────────────┐
   浏览器 / 客户端 ──HTTPS──▶ │  nginx (TLS, 反向代理, 限流, 静态资源)        │
                        └───────────────┬──────────────────────────────┘
                                        │ /api/*
                                        ▼
                        ┌──────────────────────────────────────────────┐
                        │  FastAPI 应用 (多 worker, uvicorn/gunicorn)    │
                        │  ├─ Auth 中间件: 校验 JWT → 解析 tenant_id      │
                        │  ├─ 依赖注入 get_current_tenant(): 所有查询带   │
                        │  │   tenant_id，杜绝跨租户越权                   │
                        │  ├─ REST 路由: 业务线/内容/运行/产物/报表 CRUD   │
                        │  └─ 作业 API: 提交长任务 → 返回 job_id          │
                        └───────┬───────────────────────┬───────────────┘
                                │ 同步短操作             │ 长任务投递
                                ▼                        ▼
                  ┌─────────────────────┐   ┌──────────────────────────┐
                  │ 每租户存储层          │   │ 作业队列 / 后台 Worker      │
                  │ A) data/<tid>/geo.db │   │ ThreadPool / ARQ / RQ      │
                  │    (SQLite+WAL)      │   │ 调用 GeoPipeline.run()    │
                  │ B) PostgreSQL        │   │ 写 job 状态 + 产物         │
                  │    tenant_id 分区    │   └──────────────────────────┘
                  └─────────────────────┘
                                ▲
                                │ 复用（不改一行）
                        ┌──────────────────────────────────────────────┐
                        │  geo_engine 核心: ingest/structure/semantic/    │
                        │  formats/distribute/monitor + pipeline          │
                        └──────────────────────────────────────────────┘
```

---

## 3. 租户模型与数据隔离（核心改动）

### 3.1 租户作为最外层命名空间
新增 `tenant_id`（注册时生成，UUID 或 `org_slug`）。所有「用户私有数据」都挂在 `(tenant_id, business_line)` 下：

- **方案 A（推荐 MVP）— 每租户独立 SQLite 文件**
  `data/<tenant_id>/geo.db`，原 `store.Store(db_path)` 改成 `Store.for_tenant(tenant_id)`。
  - 隔离强度最高（文件系统级），单租户备份/导出/删除 = 复制/删除一个文件；
  - 仍保持「零外部依赖」，最贴合现有架构；
  - 限制：单租户内写并发受 SQLite 限制（对 SMB 足够），跨租户聚合统计需遍历文件。
- **方案 B（规模化）— 单 PostgreSQL，`tenant_id` 列 + 行级安全(RLS)**
  - 所有表加 `tenant_id`（含现有 documents/chunks/facts/qas/terms/artifacts/probes/runs）；
  - 应用层强制 `WHERE tenant_id=?`，并用 PostgreSQL RLS 做第二道防线；
  - 跨租户报表、计量、 admin 看板天然好做。

> 实现时 `Store` 抽象成接口：`Store.for_tenant(tid)` 返回 A 或 B 的实现，业务代码不感知差异。

### 3.2 配置与产物隔离
- `ConfigRepository` 改为按租户加载：`business_lines/<tenant_id>/*.json`（或在库内 `business_lines` 表加 `tenant_id` 列）。
- 磁盘产物改挂租户目录：`content/<tid>/<bl>/`、`dist/<tid>/<bl>/`、`reports/<tid>/<bl>/`。
- **路径穿越防护**：所有 `bl_id`/`tenant_id` 经 `slugify` 规范化，拒绝 `..`、绝对路径、非白名单字符。

---

## 4. 并发访问控制

| 问题 | 方案 |
|------|------|
| SQLite 单连接锁 | A) 每租户库 + WAL(`PRAGMA journal_mode=WAL`) + 每请求从连接池取连接；B) PostgreSQL 原生并发 |
| 长任务阻塞 HTTP | 把 `run()` 包成**后台作业**：API 立即返回 `job_id`，Worker 异步执行并写 `jobs` 表（status/progress/log）；前端轮询或 SSE/WebSocket 取进度 |
| 多 worker 进程 | 无进程内共享状态；作业状态、产物、配置全部落库；`REGISTRY` import 期注册无碍 |
| 同租户同业务线并发写 | 作业级乐观锁：提交时若同 `(tid,bl)` 已有 running 作业，则排队或拒绝（409） |
| 限流 | nginx `limit_req` + 应用层按 `tenant_id` 配额（免费/付费档） |

**作业状态机**：`queued → running → (succeeded | failed)`，附 `progress`（当前阶段）、`log_url`、`result_url`。

---

## 5. 身份认证与会话管理

- **账号模型**：`users(tenant_id, user_id, email, password_hash, role)`。`role` ∈ {owner, admin, member, viewer}。
- **密码**：argon2id / bcrypt 哈希，盐随机；禁止明文、禁止弱口令。
- **鉴权方式（推荐 JWT，无状态）**：
  - 登录 `/auth/login` → 返回 `access_token`(短时效, 15min) + `refresh_token`(长时效, 可吊销)；
  - 访问令牌 `Authorization: Bearer <jwt>`，载荷含 `sub=user_id`、`tid=tenant_id`、`role`；
  - 刷新 `/auth/refresh`；登出把 refresh 加入**吊销列表**（Redis/库表）实现「会话管理」可控。
- **会话管理要点**：服务端维护 refresh 令牌表（可撤销，满足「强制下线/改密失效」）；access 短命降低泄露风险；可选设备维度审计。
- **入口强制**：FastAPI 依赖 `get_current_tenant()` 在每次请求解析并注入 `tenant_id`，所有存储/配置查询默认带该值——从框架层消除越权可能。
- 可选扩展：OAuth2/OIDC（企业微信、GitHub）接入，作为后续迭代。

---

## 6. API 设计（REST，核心引擎原样调用）

> 以下每个接口都在依赖中拿到 `tenant_id`，内部唯一区别是「多传一个 tenant 维度」。

| 方法 | 路径 | 说明 | 调用核心 |
|------|------|------|----------|
| POST | `/auth/register` | 注册租户+首个 owner | — |
| POST | `/auth/login` `/auth/refresh` `/auth/logout` | 鉴权/会话 | — |
| GET | `/business-lines` | 列出我的业务线 | `ConfigRepository`（租户作用域）|
| POST | `/business-lines` | 新建业务线（配置 JSON）| `ConfigRepository.save` |
| GET/PUT/DELETE | `/business-lines/{bl}` | 查看/改/删 | — |
| POST | `/business-lines/{bl}/sources` | 上传/录入内容（md/文本/url）| `ingest` 数据源 |
| POST | `/business-lines/{bl}/run` | **提交运行**（异步，返回 job_id）| `GeoPipeline.run`（Worker 内）|
| GET | `/jobs/{job_id}` | 任务状态/进度/日志 | `jobs` 表 |
| GET | `/business-lines/{bl}/artifacts` | 我的产物清单（llms.txt 等）| `store` / `dist/` |
| GET | `/business-lines/{bl}/reports` | 监测报表/看板 | `monitor`/`report` |
| GET | `/health` | 探活（无需鉴权）| — |

所有写接口幂等、参数经 Pydantic 校验；读接口默认只返本租户数据。

---

## 7. 存储迁移对照

- **现有表加 `tenant_id` 或切每租户库**：二选一（见 §3.1）。推荐 MVP 走 A。
- 新增 `users`、`tenants`、`refresh_tokens`、`jobs` 表（鉴权与任务）。
- 现有 CLI（`python -m geo_engine ...`）**保留不动**：继续服务本地/运维/CI；服务器端走 `geo_web`。两者共享同一 `geo_engine` 核心。
- 数据迁移脚本：把旧的全局 `business_lines/` + `data/geo.db` 导入为「默认超级租户」，保证既有成果不丢失。

---

## 8. 服务器部署与网络访问

**目标载体**：一台 Linux 服务器（您已有的腾讯云 Lighthouse 上海 4C4G，或任意 VPS/云主机）。

1. **容器化**
   - `Dockerfile`：基于 `python:3.13-slim`，装 `requirements.txt`（fastapi/uvicorn[standard]/pydantic + 选装 DB 驱动 + 鉴权库）。
   - `docker-compose.yml`：服务 `geo-web`（多 replica）+ 可选 `postgres` + `nginx` + `certbot`。
2. **反向代理与 TLS**
   - nginx：终止 TLS、HTTP→HTTPS 跳转、`/api` 反代到容器、`/static` 直出产物、全局 `limit_req` 限流、上传体大小限制。
   - Let's Encrypt（certbot）自动签发续期域名证书。
3. **进程与自愈**
   - `uvicorn` 多 worker（CPU 核数×2+1）；docker `restart: unless-stopped`；可选 systemd。
4. **配置与密钥**
   - 环境变量注入：`JWT_SECRET`、`DB_*`、`CORS_ORIGINS`、`BASE_URL`；**绝不入库/不提交**。`.env` 仅服务器本地、加入 `.gitignore`。
5. **网络**
   - 域名解析到服务器（A 记录）；开放 443/80，SSH 改非 22 或仅密钥；防火墙只放必要端口。
6. **可观测**
   - 结构化日志（已有 `logutil`）、`/health`、错误率/任务队列深度监控（可选 Prometheus）。

---

## 9. 安全清单（多用户必做）

- [ ] 全接口鉴权（除 `/health`、`/auth/login`、`/auth/register`）；失败统一 401
- [ ] 租户隔离由依赖层强制，禁止任何「不带 tenant_id 的查询」进入存储层
- [ ] 路径穿越防护（`slugify` + 白名单），`bl_id`/`tenant_id` 不可构造越权路径
- [ ] 密码 argon2id 哈希；登录失败限流 + 锁定；refresh 可吊销
- [ ] HTTPS 全程；HSTS；安全 Cookie（如需）；JWT 短命 + 刷新可撤
- [ ] 上传内容大小/类型限制；LLM/外链抓取走服务端代理、超时与域名白名单
- [ ] 密钥全部环境变量；`JWT_SECRET` 高熵随机；`.env` 不入库
- [ ] 每租户配额/限流；大任务异步化避免资源耗尽
- [ ] 审计日志（谁、何时、做了什么）；敏感操作可追溯
- [ ] 依赖定期更新 + 最小权限容器（非 root 运行）

---

## 10. 分阶段实施计划

| 阶段 | 内容 | 核心引擎改动 | 产出 | 估时 |
|------|------|--------------|------|------|
| P0 仓库拆分 | 新增 `geo_web/` 包；`requirements.txt`；CLI 保持不变 | 无 | 双入口可并行 | 0.5d |
| P1 租户存储 | `Store.for_tenant()`（每租户库/WAL 或 PG）、`ConfigRepository` 租户作用域、产物目录命名空间化、路径防护 | 无（仅外壳）| 隔离存储层 | 2–3d |
| P2 鉴权 | `users/tenants/refresh_tokens` 表、JWT 签发校验、`/auth/*`、`get_current_tenant` 依赖 | 无 | 登录/会话 | 2–3d |
| P3 API 层 | FastAPI 路由映射 §6；长任务转后台作业（ThreadPool/ARQ），job 状态接口 | 无 | REST API | 2–3d |
| P4 并发加固 | WAL/连接池或 PG、多 worker、限流、同业务线任务互斥 | 无 | 压测通过 | 1.5–2d |
| P5 部署 | Dockerfile/compose、nginx、certbot、env、runbook、数据迁移脚本 | 无 | 可上线 | 1.5–2d |
| P6 验证 | 多用户并发模拟、跨租户越权测试、负载测试、文档 | 无 | 测试报告 | 1–2d |

**总计 MVP ≈ 10–14d；含 PG/任务队列/计量 ≈ 18–25d。**

---

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| SQLite 并发写瓶颈 | MVP 用每租户库+WAL；若用户量大/写频繁，切 PG（接口已抽象）|
| 长任务占用资源拖垮服务 | 后台作业 + 配额 + 超时；重任务可限制并发数 |
| 跨租户数据泄露（最严重）| 依赖层强制 `tenant_id`；存储层拒绝无租户查询；越权测试纳入 CI |
| LLM/外链抓取被滥用（SSRF）| 服务端代理 + 域名白名单 + 超时 |
| 密钥泄露 | 环境变量 + 最小权限 + 定期轮换 |
| 云服务器「价值有限」顾虑 | 本方案与「能否访问本地文件」无关；纯服务端运行，Lighthouse 4C4G 足够跑 MVP；亦可换任意 VPS |

---

## 12. 决策点（请确认后再进入实现）

1. **存储方案**：A) 每租户 SQLite（零额外依赖，推荐 MVP） / B) PostgreSQL（规模化，需装数据库）
2. **部署目标**：您的腾讯云 Lighthouse（上海 4C4G）/ 新购 VPS / 内网服务器（仅公司内访问）
3. **Web 框架**：FastAPI（推荐，异步+自动文档）/ Flask（若团队更熟）
4. **鉴权**：JWT 无状态（推荐）/ 服务端 Session（需共享会话存储）
5. **后台任务**：进程内线程池（MVP 够用）/ ARQ-Redis 或 Celery（规模化）

> 核心引擎 `geo_engine` 在所有方案下均**原样复用、不改算法**，改动只发生在新增的 `geo_web` 与存储外壳层。

---

## 13. 实施进度（2026-08-30 已完成 P0 + P1 + OAuth）

已按本文决策实现并验证通过：

- **P0（geo_web 外壳层）**：`geo_web/` 包（app/server/schemas/deps/jobs/control/tenant/security/auth_providers/__init__）+ `requirements.txt` + `.env.example` + `Dockerfile`。CLI 与核心引擎保持不变。
- **P1（租户存储隔离）**：`Store` 启用 WAL + 新增 `jobs/users/refresh_tokens` 表；`control.py` 控制库（tenants + id_map + user_index）；`tenant.py` 租户上下文 + 路径穿越防护 `validate_id` + `Store.for_tenant` 进程内缓存；`Settings.layout` 把数据根指向 `tenants/<tid>`，业务线/内容/产物**天然**按租户隔离。
- **认证核心**：`security.py` 纯标准库 HS256 JWT + PBKDF2 口令哈希；`deps.get_current_tenant` 从 Bearer 解析租户并二次校验控制库。
- **OAuth / 企业微信**：`auth_providers.py` 抽象 + `WeComProvider`（真实流程 + `test_mode` 免凭据）+ `DevProvider`；`/api/auth/oauth/{provider}/start|callback` 首次登录自动开通租户。
- **后台作业**：`jobs.py` 线程池投递，状态/进度/结果落租户库 `jobs` 表，并自动把产物落盘到租户 `dist`。

验证结果：
- `tests/test_tenant_isolation.py`（纯标准库，7 项）：读隔离、目录命名空间、路径穿越拦截、并发 WAL、OAuth 映射、JWT/口令 —— **全过**。
- `tests/test_api_smoke.py`（fastapi TestClient，4 项）：register→me→建业务线→上传内容→后台作业→轮询成功→列出 llms.txt、OAuth dev 流程、跨租户隔离 —— **全过**。
- `python -m geo_web.server` 实测启动，`/api/health` 正常，未带令牌访问受保护接口返回 **401**。

接口与部署详情见 [docs/geo-web-api.md](docs/geo-web-api.md)；后续 PostgreSQL 分支 / Redis 刷新索引 / OAuth state 绑定属规模化增强项，不在 MVP 范围内。

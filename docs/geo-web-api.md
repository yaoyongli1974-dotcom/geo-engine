# GEO Web API —— 多用户服务器端接口说明

在**不改核心引擎 `geo_engine`** 的前提下，`geo_web` 包提供多用户服务器端能力：租户隔离、JWT 鉴权、可插拔 OAuth（企业微信/Dev）、后台作业与 REST 接口。

## 1. 架构与隔离边界

```
Client ──(HTTPS/TLS, nginx)── FastAPI
   │  依赖 get_current_tenant 注入租户上下文
   ├── 控制库 control.db        租户登记 + 外部身份映射(user_index/id_map)
   └── 各租户库 tenants/<tid>/geo.db  业务数据 + 用户 + 作业 + artifacts 表
           产物文件 tenants/<tid>/dist/<bl>/  按租户物理隔离
           长任务 → 线程池 JobManager，状态落租户库 jobs 表
```

- **路径穿越防护**：`validate_id` 拒绝 `..`/绝对路径/非法字符；产物服务用 `normpath` 前缀校验。
- **跨租户隔离**：所有存储/配置/产物默认带 `tenant` 维度；JWT 内 `tid` 经控制库二次校验。

## 2. 安装与运行

```bash
pip install -r requirements.txt
export GEO_DATA_DIR=/var/lib/geo/data      # 持久卷（务必外挂）
export GEO_JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
python -m geo_web.server                    # 默认 0.0.0.0:8000
# 或生产多进程：
uvicorn geo_web.server:app --host 0.0.0.0 --port 8000 -w 4
```

容器化：`docker build -t geo-web . && docker run -e GEO_JWT_SECRET=... -v geo-data:/data/geo -p 8000:8000 geo-web`

## 3. 认证流程

### 3.1 本地注册 / 登录
- `POST /api/auth/register` `{org_name, email, name, password}` → 自动创建租户 + owner 用户，返回 `access_token`/`refresh_token`。
- `POST /api/auth/login` `{email, password}` → 返回令牌。
- `POST /api/auth/refresh` `{refresh_token}` → 轮换新令牌（旧 refresh 立即吊销）。
- `POST /api/auth/logout` → 无状态令牌返回提示（短过期自动失效）。
- 所有业务接口需 `Authorization: Bearer <access_token>`。

### 3.2 OAuth / 企业微信
- `GET /api/auth/oauth/{provider}/start` → 返回 `authorize_url`（跳转企业微信扫码）。
- `GET /api/auth/oauth/{provider}/callback?code=&state=` → 用 `code` 换取外部身份，**首次自动开通租户与用户**，返回令牌。
- 提供方：`wecom`（企业微信，配置 `GEO_WECOM_CORPID/CORPSECRET/AGENTID`；缺省回落 `test_mode`）、`dev`（测试用，免网络）。
- 扩展新提供方：继承 `geo_web.auth_providers.OAuthProvider` 实现 `authorize_url`/`exchange`，到 `PROVIDERS` 工厂登记。

> 企业微信真实流程：corpid+corpsecret 取 access_token → getuserinfo(code) 得 userid → user/get 得 name/email。
> 生产建议：OAuth `state` 绑定会话/CSRF 令牌；本 MVP 不强制，文档已标注。

## 4. 业务接口（均需 Bearer）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查（无需鉴权） |
| GET | `/api/me` | 当前租户/用户信息 |
| GET | `/api/business-lines` | 列出本租户业务线 |
| POST | `/api/business-lines` | 新建业务线（自动挂 `content/<id>` 内容源） |
| GET | `/api/business-lines/{bl}` | 业务线配置 |
| PUT | `/api/business-lines/{bl}/content` | 上传一篇内容（写 `content/<bl>/<slug>.md`） |
| POST | `/api/business-lines/{bl}/run` | 触发后台作业，返回 `job_id`（202） |
| GET | `/api/jobs/{job_id}` | 查询作业状态/进度/结果 |
| GET | `/api/business-lines/{bl}/artifacts` | 列出产物（llms.txt 等） |
| GET | `/api/artifacts/{bl}/{path}` | 下载/预览指定产物（路径穿越防护） |
| GET | `/api/business-lines/{bl}/report` | 最新监测报表 |

## 5. 调用示例

```bash
# 注册
TOK=$(curl -s -X POST localhost:8000/api/auth/register -H 'Content-Type: application/json' \
  -d '{"org_name":"示例公司","email":"ops@example.com","name":"管理员","password":"Str0ng!Pass"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOK"

# 建业务线 + 上传内容
curl -s -X POST localhost:8000/api/business-lines -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"id":"demo","name":"示例业务线"}'
curl -s -X PUT localhost:8000/api/business-lines/demo/content -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"title":"关于我们","content":"# 关于我们\n我们提供弱电智能化集成服务。","authority":3}'

# 运行（异步）
JOB=$(curl -s -X POST localhost:8000/api/business-lines/demo/run -H "$AUTH" \
  -d '{"use_llm":false,"force":true}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 轮询结果
curl -s localhost:8000/api/jobs/$JOB -H "$AUTH"
```

## 6. 测试

```bash
# 多租户隔离验证（纯标准库，不需 fastapi）
python -m tests.test_tenant_isolation
# API 端到端冒烟（需先 pip install -r requirements.txt）
python -m tests.test_api_smoke
```

## 7. 已知边界（MVP）
- `refresh` 定位租户采用「遍历控制库已登记租户」的简化策略；生产建议把 refresh 索引进控制库或 Redis。
- 无状态 JWT 无法主动吊销，依赖短过期 + refresh 轮转；如需即时吊销可引入吊销列表。
- 默认每租户单 SQLite + WAL；高并发单租户写请考虑 PostgreSQL 分支（见架构文档）。

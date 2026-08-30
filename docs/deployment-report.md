# GEO 引擎远程部署报告

> 部署目标：Ubuntu 24.04.4 LTS（152.32.135.156 / 10.7.88.210）  
> 部署时间：2026-08-30  
> 部署版本：geo-engine（多用户服务器端改造版，commit 3f68d5b）

---

## 1. 部署摘要

| 检查项 | 状态 | 说明 |
| --- | --- | --- |
| SSH 环境探查 | 正常 | 已确认 1Panel、OpenResty、MySQL、Docker 运行状态 |
| 组件复用 | 正常 | 复用 OpenResty 做反向代理，复用系统 Python 3.12.3 + Docker |
| 代码上传 | 正常 | 已部署到 `/opt/geo-engine` |
| 依赖安装 | 正常 | 使用独立 venv，安装 requirements.txt 全部依赖 |
| 环境变量 | 正常 | `.env` 已生成，JWT 密钥随机生成并 600 权限 |
| 进程守护 | 正常 | `geo-engine.service` 已启用并运行 |
| 反向代理 | 正常 | OpenResty 80 端口反代到 127.0.0.1:8000 |
| 防火墙 | 正常 | 8000 不对外暴露，仅 80/443 对外；ufw 已放行 80/443 |
| 访问验证 | 正常 | `http://152.32.135.156/api/health` 返回 200 |
| 认证验证 | 正常 | 注册接口返回 access_token/refresh_token |
| SSH 安全加固 | 正常 | 已部署密钥、禁用密码登录 |
| 域名 HTTPS | 正常 | geo.xb168.com 已解析并配置 HTTP→HTTPS 重定向，Let's Encrypt 证书有效期至 2026-11-28 |

---

## 2. 服务器环境

```text
系统：Ubuntu 24.04.4 LTS (noble)
内核：6.8.0-138-generic
架构：x86_64
外网 IP：152.32.135.156
内网 IP：10.7.88.210
主机名：10-7-88-210
CPU：2 核
内存：3.8 GiB
磁盘：19 GB（已用 15 GB，剩余 3.6 GB）
```

### 已安装组件（复用）

| 组件 | 版本/状态 | 用途 |
| --- | --- | --- |
| 1Panel | 运行中（1panel-agent/core） | 面板管理 |
| OpenResty | 1.31.1.1（host 网络容器） | 反向代理 |
| MySQL | 8.0.46（容器，仅 127.0.0.1:3306） | 当前未使用，保留备用 |
| Docker | 29.7.2 | 容器运行时 |
| Python | 3.12.3（系统） | GEO 引擎运行时 |
| ufw | active | 防火墙 |

### 端口占用

```text
22    SSH
53    systemd-resolved
80    OpenResty HTTP
443   OpenResty HTTPS
3306  MySQL（容器，127.0.0.1）
5678  n8n（127.0.0.1）
8443  1Panel 安全入口
9000  PHP-FPM
23456 1Panel
54321 x-ui
```

---

## 3. 部署目录与文件

```text
/opt/geo-engine
├── .env                    # 环境变量（权限 600）
├── venv/                   # Python 虚拟环境
├── geo_web/                # FastAPI 多用户外壳
├── geo_engine/             # GEO 核心引擎（零改动）
├── business_lines/         # 示例业务线配置
├── config.json             # 引擎配置
├── requirements.txt
├── Dockerfile
├── docs/                   # 项目文档
├── tests/                  # 测试脚本
└── data/                   # 持久化数据目录
    ├── control.db          # 租户/用户/身份映射库
    └── tenants/<tid>/      # 各租户隔离数据
```

---

## 4. 环境变量配置

文件位置：`/opt/geo-engine/.env`

```bash
GEO_DATA_DIR=/opt/geo-engine/data
GEO_ACCESS_MIN=15
GEO_REFRESH_DAYS=7
PORT=8000
HOST=127.0.0.1
GEO_BASE_URL=http://152.32.135.156
GEO_CORS_ORIGINS=*
GEO_JOB_WORKERS=2
PROXY_HEADERS=1
GEO_JWT_SECRET=<随机生成，长度 64，已写入 .env>
```

> **注意**：`GEO_JWT_SECRET` 已在服务器本地随机生成，未在本文档或任何聊天记录中明文保存。如需重置，需重新生成并重启服务。

---

## 5. 访问地址

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 健康检查（域名 HTTPS） | `https://geo.xb168.com/api/health` | 无需鉴权，强制 HTTPS |
| API 根（域名 HTTPS） | `https://geo.xb168.com/api/` | 需 Bearer Token |
| 交互式文档 | `https://geo.xb168.com/docs` | Swagger UI |
| OpenAPI 规范 | `https://geo.xb168.com/openapi.json` | 自动生成的接口定义 |
| 健康检查（IP HTTP，备用） | `http://152.32.135.156/api/health` | 无需鉴权，仅 HTTP |

> **域名访问**：`geo.xb168.com` 已解析到 `152.32.135.156`，并启用 Let's Encrypt 证书（HTTP 自动 301 跳转到 HTTPS）。所有 API 调用请使用 `https://geo.xb168.com/...`。IP 直连（152.32.135.156）仍仅提供 HTTP，作为内网/备用入口。

---

## 6. 服务状态检查

### 6.1 查看服务状态

```bash
ssh -i ~/.workbuddy/keys/geo-engine-deploy ubuntu@152.32.135.156
sudo systemctl status geo-engine --no-pager
```

### 6.2 查看实时日志

```bash
sudo journalctl -u geo-engine -f
```

### 6.3 查看最近日志

```bash
sudo journalctl -u geo-engine -n 50 --no-pager
```

### 6.4 本地健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
```

### 6.5 外网健康检查

```bash
curl -s http://152.32.135.156/api/health
```

### 6.6 检查反向代理配置

```bash
sudo docker exec 1Panel-openresty-GeCi nginx -T | grep -E "server_name|proxy_pass" | head -n 20
```

---

## 7. 反向代理配置

OpenResty 由 1Panel 以容器方式管理（`1Panel-openresty-GeCi`），其 `conf.d` 在宿主机挂载点为 `/opt/1panel/www/conf.d/`，容器内为 `/usr/local/openresty/nginx/conf/conf.d/`。

> ⚠️ **重要**：修改反向代理配置**只能写 `/opt/1panel/www/conf.d/` 下的文件**。1Panel 会在保存/重载时用它自己的模板重新渲染它所管理的网站配置（如 `geo-engine.conf`），因此**不要直接手写 `geo-engine.conf` 的 `server_name` 等字段**——会被覆盖。域名相关的独立配置应使用单独的文件名（如 `geo-xb168.com.conf`），1Panel 不会覆盖它。

### 7.1 IP 访问（1Panel 管理，勿手动改 server_name）

- 文件：`/opt/1panel/www/conf.d/geo-engine.conf`（1Panel 渲染，仅 `server_name 152.32.135.156`）
- 容器内：`/usr/local/openresty/nginx/conf/conf.d/geo-engine.conf`

### 7.2 域名 HTTPS（geo.xb168.com，独立配置）

- 文件：`/opt/1panel/www/conf.d/geo-xb168.com.conf`
- 逻辑：80 端口对 `/.well-known/acme-challenge` 放行（证书续期用），其余 301 跳 HTTPS；443 端口启用 Let's Encrypt 证书反代到 `127.0.0.1:8000`。

```nginx
server {
    listen 80;
    server_name geo.xb168.com;
    location ^~ /.well-known/acme-challenge { root /usr/share/nginx/html; allow all; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    server_name geo.xb168.com;
    ssl_certificate /usr/local/openresty/nginx/conf/ssl/geo_xb168_com/fullchain.pem;
    ssl_certificate_key /usr/local/openresty/nginx/conf/ssl/geo_xb168_com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location /api/ { proxy_pass http://127.0.0.1:8000/api/; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    # /docs/ 映射到后端 /docs：FastAPI 已关闭 redirect_slashes，Nginx 又会把 /docs 301 到 /docs/，
    # 若 proxy_pass 带尾斜杠则后端收到 /docs/ 会 404，故去掉尾斜杠。
    location /docs/ { proxy_pass http://127.0.0.1:8000/docs; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location /openapi.json { proxy_pass http://127.0.0.1:8000/openapi.json; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location / { return 200 'GEO Engine is running\n'; add_header Content-Type text/plain; }
}
```

证书存放（宿主机）：`/opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/`，容器内对应 `/usr/local/openresty/nginx/conf/ssl/geo_xb168_com/`。

ACME webroot（宿主机）：`/opt/1panel/apps/openresty/openresty/root`（容器内 `/usr/share/nginx/html`）。

重载命令：

```bash
sudo docker exec 1Panel-openresty-GeCi openresty -t
sudo docker exec 1Panel-openresty-GeCi openresty -s reload
```

---

## 8. 防火墙/安全组

当前策略：

- `geo-engine` 仅监听 `127.0.0.1:8000`，不对外暴露，无需放行 8000。
- OpenResty 占用 80/443 并对外提供访问，ufw 已放行。

```bash
sudo ufw status numbered
```

如需未来直接暴露 8000 端口（不推荐），可执行：

```bash
sudo ufw allow 8000/tcp
```

---

## 9. 后续更新部署步骤

### 9.1 增量更新（推荐）

1. 本地开发/修改代码后提交到 GitHub。
2. 登录服务器拉取最新代码：

```bash
ssh -i ~/.workbuddy/keys/geo-engine-deploy ubuntu@152.32.135.156
cd /opt/geo-engine
git pull origin main
```

3. 如依赖有变化，更新 venv：

```bash
venv/bin/pip install -r requirements.txt -q
```

4. 重启服务：

```bash
sudo systemctl restart geo-engine
sudo systemctl status geo-engine --no-pager
```

### 9.2 全量重新部署

如需彻底重新部署：

```bash
ssh -i ~/.workbuddy/keys/geo-engine-deploy ubuntu@152.32.135.156
sudo systemctl stop geo-engine
sudo rm -rf /opt/geo-engine
# 然后重新上传代码、安装依赖、创建 .env、启动服务
```

> **警告**：`rm -rf /opt/geo-engine` 会删除 `data/` 目录中的所有租户数据，请先备份。

### 9.3 备份数据

```bash
# 备份到用户目录
sudo tar -czf /home/ubuntu/geo-engine-backup-$(date +%Y%m%d-%H%M%S).tar.gz /opt/geo-engine/data
```

---

## 10. 敏感信息处理

### 10.1 已采取的措施

1. **SSH 密码**：用户提供的临时密码仅用于本次部署；部署完成后已禁用密码登录，改用 ED25519 密钥。
2. **JWT 密钥**：`GEO_JWT_SECRET` 在服务器本地随机生成，未通过聊天、脚本或文档明文传输/保存。
3. **环境变量文件**：`/opt/geo-engine/.env` 权限设置为 `600`，仅 `ubuntu` 用户可读。
4. **数据库**：当前使用 SQLite 本地文件，未启用远程 MySQL，减少攻击面。
5. **反向代理**：内部服务不对外暴露，仅通过 OpenResty 80 端口访问。

### 10.2 仍需注意的风险

- `.env` 文件仍位于服务器磁盘上，请确保服务器磁盘加密或访问控制严格。
- 聊天记录中的 SSH 密码已失效（密码登录已禁用），但建议不要长期保留相关聊天记录。
- 已为 `geo.xb168.com` 启用 HTTPS（Let's Encrypt，自动续期）。IP 直连（152.32.135.156）仍为 HTTP，如需也可在 1Panel 中为 IP 申请自签/CA 证书（可选，非必须）。

---

## 11. SSH 密钥登录说明

### 11.1 密钥位置

- 私钥：`C:\Users\h\.workbuddy\keys\geo-engine-deploy`
- 公钥：`C:\Users\h\.workbuddy\keys\geo-engine-deploy.pub`
- 已部署到服务器：`/home/ubuntu/.ssh/authorized_keys`

### 11.2 登录命令

```bash
ssh -i ~/.workbuddy/keys/geo-engine-deploy ubuntu@152.32.135.156
```

> Windows 用户请将 `~/.workbuddy/keys/geo-engine-deploy` 替换为实际路径，例如 `C:\Users\h\.workbuddy\keys\geo-engine-deploy`。

### 11.3 密码登录状态

```bash
# 已禁用
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AuthenticationMethods publickey
```

---

## 12. 已知限制与后续建议

1. **TLS/HTTPS**：已为 `geo.xb168.com` 启用（见 §14）。如还需为 IP 或其他域名开启 HTTPS，可在 1Panel 中申请证书并按 §7.2 方式追加 server 块。
2. **域名绑定**：`geo.xb168.com` 已生效。如需使用 `geo.xalcy.cn` 或 `www.xalcy.cn`，请将域名解析到 152.32.135.156，然后复制 §7.2 的 `geo-xb168.com.conf` 为对应域名文件并重新签发证书即可。
3. **数据库存储**：当前使用 SQLite，适合中小规模。如租户/并发量增长，可迁移到 MySQL/PostgreSQL。
4. **磁盘空间**：系统盘仅剩 3.6 GB，建议监控并清理日志，或扩容。
5. **日志轮转**：当前未配置 `logrotate`，长期运行请为 `/var/log/journal` 或 systemd 日志设置保留策略。
6. **监控告警**：建议配置 `systemd` 服务异常通知，或接入 1Panel 的监控告警。
7. **OAuth 企业微信**：当前为测试模式（DevProvider）。如需真实企业微信登录，请配置 `GEO_WEBCOM_CORPID`、`GEO_WEBCOM_CORPSECRET`、`GEO_WEBCOM_AGENTID`。

---

## 13. 验证结果记录

```text
$ curl -s http://152.32.135.156/api/health
{"status":"ok","service":"geo-web","tenants":1,"time":"2026-08-30T11:32:00+00:00"}

$ curl -s http://152.32.135.156/api/business-lines -H "Authorization: Bearer <token>"
# 返回当前租户的业务线列表

$ curl -s http://152.32.135.156/
GEO Engine is running
```

---

## 14. 域名与 HTTPS 部署（geo.xb168.com）

### 14.1 DNS 解析

`geo.xb168.com` 已添加 A 记录指向 `152.32.135.156`（服务器本地 `/etc/hosts` 亦已同步）。

```bash
nslookup geo.xb168.com   # -> 152.32.135.156
```

### 14.2 证书签发（Let's Encrypt，acme.sh）

证书工具 `acme.sh` 安装在服务器 `/home/ubuntu/.acme.sh/`，使用 webroot 模式（无需停服）：

- Webroot（宿主机）：`/opt/1panel/apps/openresty/openresty/root`（容器内 `/usr/share/nginx/html`）
- 证书部署目录（宿主机）：`/opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/`
- 签发命令：

```bash
sudo HOME=/home/ubuntu /home/ubuntu/.acme.sh/acme.sh --issue \
  -d geo.xb168.com --webroot /opt/1panel/apps/openresty/openresty/root --server letsencrypt

sudo HOME=/home/ubuntu /home/ubuntu/.acme.sh/acme.sh --install-cert -d geo.xb168.com \
  --cert-file /opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/cert.pem \
  --key-file  /opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/privkey.pem \
  --fullchain-file /opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/fullchain.pem \
  --reloadcmd "docker exec 1Panel-openresty-GeCi openresty -s reload"
```

### 14.3 自动续期

`acme.sh` 安装时已写入 `ubuntu` 用户的 cron（每 6 小时检查，到期前 30 天自动续期）：

```text
39 4,10,16,22 * * * "/home/ubuntu/.acme.sh"/acme.sh --cron --home "/home/ubuntu/.acme.sh" > /dev/null
```

续期后通过 `--reloadcmd` 执行 `docker exec 1Panel-openresty-GeCi openresty -s reload` 平滑重载。**已确认 `ubuntu` 用户已加入 `docker` 组**，续期时的 reload 可正常执行，无需人工干预。

证书有效期：约 90 天（本次 `2026-08-30` → `2026-11-28`）。

### 14.4 重定向策略

- `http://geo.xb168.com/*`（除 `/.well-known/acme-challenge`）→ `301` 重定向到 `https://geo.xb168.com/*`
- `https://geo.xb168.com/*` → 反代 `127.0.0.1:8000`
- IP 直连 `http://152.32.135.156/*` 仍保持 HTTP（1Panel 管理的 `geo-engine.conf`）

### 14.5 验证命令

```bash
# HTTP 应 301 跳转
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' http://geo.xb168.com/api/health
# HTTPS 应 200
curl -s https://geo.xb168.com/api/health
# 证书信息
sudo openssl x509 -in /opt/1panel/apps/openresty/openresty/conf/ssl/geo_xb168_com/fullchain.pem -noout -dates -subject
```

### 14.6 `/docs` 重定向循环修复

**现象**：浏览器访问 `https://geo.xb168.com/docs` 出现 `ERR_TOO_MANY_REDIRECTS`（该网页无法正常运作，将您重定向的次数过多）。

**原因**：

1. Nginx 默认会把不带斜杠的 `/docs` 301 补全到 `/docs/`。
2. FastAPI 默认又会把 `/docs/` 307 重定向回 `/docs`（`redirect_slashes=True`）。
3. 两者叠加形成 `/docs` → `/docs/` → `/docs` 无限循环。

**修复**：

1. **后端**：在 `geo_web/app.py` 创建 FastAPI 应用时关闭斜杠重定向：

```python
app = FastAPI(title="GEO Web API", version="1.0.0", redirect_slashes=False)
```

2. **反向代理**：Nginx 的 `/docs/` location 中 `proxy_pass` 去掉尾斜杠，使 Nginx 补全后的 `/docs/` 请求映射到后端的 `/docs`：

```nginx
location /docs/ {
    proxy_pass http://127.0.0.1:8000/docs;  # 注意无尾斜杠
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**验证**：

```bash
curl -I -L -k --max-redirs 10 "https://geo.xb168.com/docs"
# 预期：HTTP/1.1 301 -> /docs/，然后 HTTP/1.1 200 OK，无循环。
```

完整配置文件已纳入版本控制：`deploy/nginx/geo-xb168.com.conf`。

---

## 15. 可视化发布平台（Web UI）

上线后，GEO 引擎从「纯 API 服务」升级为「带可视化操作界面的在线发布平台」，非技术用户可经浏览器完成业务线配置、内容撰写、一键生成发布与公开链接管理。

### 15.1 访问地址
- 平台首页（SPA）：`https://geo.xb168.com/` 或 `https://geo.xb168.com/app`
- 交互式 API 文档：`https://geo.xb168.com/docs`

### 15.2 功能清单
| 模块 | 能力 |
| --- | --- |
| 账号 | 注册 / 登录（JWT），每账号自动创建独立租户空间，数据隔离 |
| 业务线 | 列表 / 新建 / 编辑 / 删除；配置语言、主题、目标受众 |
| 内容 | Markdown 在线编辑器；列表 / 新建 / 编辑 / 删除 |
| 生成发布 | 一键运行 GEO 流水线，生成 llms.txt / JSON-LD / 站点 / sitemap / cards 等产物，自动发布到公开目录 |
| 发布管理 | 公开 URL 列表与一键复制、发布历史、作业进度实时轮询 |

### 15.3 公开 URL 格式（GEO 核心价值）
发布成功后，以下地址**无需登录**即可访问，可直接被 ChatGPT / Perplexity / Bing 等生成式引擎抓取引用：
- `https://geo.xb168.com/p/{业务线}/llms.txt` — 面向 LLM 的站点索引
- `https://geo.xb168.com/p/{业务线}/llms-full.txt` — 完整正文
- `https://geo.xb168.com/p/{业务线}/index.html` — 生成式站点首页
- `https://geo.xb168.com/p/{业务线}/sitemap.xml` — 站点地图
- `https://geo.xb168.com/p/{业务线}/data/*.jsonld` — 结构化数据（JSON-LD）

### 15.4 架构
- FastAPI 同时托管四类路由：`/api/*`（业务，需鉴权）、`/p/*`（公开产物，无鉴权）、`/` 与 `/app`（SPA 首页）、`/static`（静态资源）。
- OpenResty 443 改为**全量反代**到 `127.0.0.1:8000`（catch-all），仅保留 `/.well-known/acme-challenge`；所有路由分发交给 FastAPI。
- 发布实现：`POST /api/business-lines/{bl}/publish` 触发异步作业；作业成功后 `jobs._publish_artifacts` 把 `dist/` 产物复制到 `DATA_DIR/published/{bl}/`，公开路由 `GET /p/{bl}/{path}` 直接 serve。

### 15.5 代码位置
- 前端：`geo_web/static/index.html`（零 CDN 依赖单文件 SPA，内联 CSS/JS）
- 后端新增：`geo_web/app.py`（内容CRUD、业务线更新/删除、发布、公开路由、静态托管）、`geo_web/jobs.py`（发布复制逻辑）、`geo_engine/store.py`（publishes 表）、`geo_web/schemas.py`（请求/响应模型）

### 15.6 更新部署
> 重要：本服务器 `/opt/geo-engine` **不是 git 仓库**（首版经 scp 上传），因此更新需手动上传文件后重启，或先 `git init` 化为 git 仓库。

```bash
# 本地改动后，经 scp 上传（示例如下），再重启
scp geo_web/app.py geo_web/jobs.py geo_web/schemas.py geo_web/__init__.py \
    ubuntu@152.32.135.156:/opt/geo-engine/geo_web/
scp geo_web/static/index.html ubuntu@152.32.135.156:/opt/geo-engine/geo_web/static/
scp geo_engine/store.py ubuntu@152.32.135.156:/opt/geo-engine/geo_engine/
ssh ubuntu@152.32.135.156 "sudo systemctl restart geo-engine"
```

### 15.7 注意事项
- **LLM 语义增强**：需配置 `GEO_LLM_*` 环境变量才有语义增强；未配置时 `use_llm=false` 仍可生成结构化产物（已端到端验证：生成 17 个产物并发布成功）。
- **公开产物不鉴权**：`/p/*` 对所有访问者开放，请勿发布涉密业务线内容。
- **Token 时效**：前端 JWT 默认 15 分钟过期，过期自动跳转登录页。
- **多用户隔离**：每个注册账号独立租户，业务线 / 内容 / 产物按租户目录隔离（`DATA_DIR/tenants/<tid>/`）。

---

**部署完成。**

---

## 16. 用户自配置大模型 + 联网搜索（AI 助手）

允许登录用户在「个人设置 → AI 配置」中自行接入自己的大模型 API 密钥，随后在内容编辑器内一键使用 AI 完善 / 生成内容，并支持调用 AI 联网搜索、整合外部资料用于创作。

### 16.1 功能清单
- **密钥自配置**：供应商预设 OpenAI / DeepSeek / Kimi(Moonshot) / Ollama(本地) / Perplexity / 自定义（OpenAI 兼容协议）。填写 API Key + 模型即可。
- **安全存储**：API Key 以 Fernet(AES-128-CBC+HMAC) 加密落库（`ai_settings` 表），主密钥来自 `GEO_MASTER_KEY`（缺省派生自 `JWT_SECRET`）；对外接口只返回脱敏片段（如 `sk-••••ABCD`），明文不出现在任何响应。
- **连通性校验**：「测试连接」用一次最小调用验证密钥可用，结果写入 `validated` 状态。
- **三类 AI 能力**（均为后台作业，前端进度轮询）：
  - `POST /api/ai/complete` —— 完善 / 续写 / 润色给定内容；
  - `POST /api/ai/generate` —— 按主题生成结构化内容草稿；
  - `POST /api/ai/research` —— 联网搜索并整合为带出处引用的调研内容。
- **联网搜索两种路径**：① 模型自带联网（Perplexity 直接返回联网答案含引用；OpenAI 走 `/v1/responses` + `web_search_preview` 工具）；② 显式搜索 API（Tavily / Brave）检索后由你的模型整合。未配置时给出明确提示。
- **错误处理**：密钥缺失 / 网络超时 / 模型返回非 2xx / 搜索无结果，均被封装为 `AIError` 并以任务失败原因返回前端，绝不 500 静默崩溃。

### 16.2 代码位置
- `geo_web/crypto.py` —— 密钥加密 / 解密 / 脱敏（依赖 `cryptography`）。
- `geo_web/ai.py` —— LLM 客户端 + 供应商预设 + 联网搜索与整合（`AIError`）。
- `geo_web/ai_jobs.py` —— AI 任务后台管理器（复用 `jobs` 表）。
- `geo_web/schemas.py` —— `AISettingsIn/Out`、`AIValidateResponse`、`AI*Request`。
- `geo_web/app.py` —— `/api/ai/settings`（GET/PUT/DELETE）、`/api/ai/settings/validate`、`/api/ai/{complete,generate,research}`。
- `geo_engine/store.py` —— 新增 `ai_settings` 表及 `get/save/delete_ai_settings`。
- `geo_web/static/index.html` —— 「AI 配置」视图（顶栏入口 `#/ai`）+ 编辑器内 AI 工具栏（完善 / 生成 / 联网搜索）。

### 16.3 更新部署（相对 §15）
1. 服务器 venv 补齐依赖：`pip install -r geo_web/requirements.txt`（**新增 `cryptography`**，密钥加密必需）。
2. 上传改动文件：`geo_web/crypto.py`、`geo_web/ai.py`、`geo_web/ai_jobs.py`、`geo_web/schemas.py`、`geo_web/app.py`、`geo_web/static/index.html`，以及 `geo_engine/store.py`、新建 `geo_web/requirements.txt`。
3. `sudo systemctl restart geo-engine`。
4. 建议设置环境变量 `GEO_MASTER_KEY`（高熵随机串）以提升密钥加密强度；未设置时回落到由 `JWT_SECRET` 派生（功能可用，但生产建议独立主密钥）。

### 16.4 注意事项
- **密钥安全**：加密主密钥与 JWT 同信任边界；生产务必设置独立的 `GEO_MASTER_KEY` 并通过 `.env`（权限 600）注入，切勿入库。
- **公开产物不鉴权**：`/p/*` 仍对所有人开放，AI 生成内容同样遵循此约束，勿在业务线中放入涉密信息。
- **联网搜索合规**：使用 Tavily / Brave 等第三方搜索服务请遵守其服务条款与配额；模型自带联网（Perplexity/OpenAI）同理。

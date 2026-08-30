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
| 健康检查 | `http://152.32.135.156/api/health` | 无需鉴权 |
| API 根 | `http://152.32.135.156/api/` | 需 Bearer Token |
| 交互式文档 | `http://152.32.135.156/docs` | Swagger UI |
| OpenAPI 规范 | `http://152.32.135.156/openapi.json` | 自动生成的接口定义 |

> 当前未绑定域名。如需绑定 `geo.xalcy.cn` 或 `www.xalcy.cn`，请在 DNS 解析到 152.32.135.156 后，修改 OpenResty 配置中的 `server_name`。

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

OpenResty 配置文件位置：

- 持久化：`/opt/1panel/www/conf.d/geo-engine.conf`
- 容器内生效：`/usr/local/openresty/nginx/conf/conf.d/geo-engine.conf`

核心规则：

```nginx
server {
    listen 80;
    server_name 152.32.135.156;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /docs/ {
        proxy_pass http://127.0.0.1:8000/docs/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /openapi.json {
        proxy_pass http://127.0.0.1:8000/openapi.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

重载命令：

```bash
sudo docker exec 1Panel-openresty-GeCi nginx -t
sudo docker exec 1Panel-openresty-GeCi nginx -s reload
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
- 当前使用 HTTP 明文传输，如需生产使用，请为 152.32.135.156 或绑定域名申请 TLS 证书。

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

1. **TLS/HTTPS**：当前仅启用 HTTP。如需 HTTPS，可在 1Panel 中为 152.32.135.156 或绑定域名申请 Let's Encrypt 证书，并修改 OpenResty 配置。
2. **域名绑定**：当前使用 IP 访问。建议将 `geo.xalcy.cn` 或 `www.xalcy.cn` 解析到 152.32.135.156，并更新 `GEO_BASE_URL` 和 `GEO_CORS_ORIGINS`。
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

**部署完成。**

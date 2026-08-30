"""Geo Web 服务入口。

用法：
    python -m geo_web.server                 # 默认 0.0.0.0:8000
    GEO_DATA_DIR=/data/geo PORT=8080 python -m geo_web.server
或：
    uvicorn geo_web.server:app --host 0.0.0.0 --port 8000

生产部署建议经 nginx 反代 + TLS；多进程用 gunicorn -k uvicorn.workers.UvicornWorker。
"""

from __future__ import annotations

import os

from .app import app

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")


def main() -> None:
    import uvicorn

    # 注意：GEO_JWT_SECRET 等敏感配置应在部署环境注入，切勿留默认值上线
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

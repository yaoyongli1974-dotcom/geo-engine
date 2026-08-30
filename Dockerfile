# GEO Web 服务镜像（多阶段，基于官方 Python）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GEO_DATA_DIR=/data/geo

WORKDIR /app

# 仅安装 Web 层依赖；核心引擎为纯标准库
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 持久数据卷（control.db + tenants/ 应落在此）
VOLUME ["/data/geo"]

EXPOSE 8000

# 单容器多进程可用：gunicorn -k uvicorn.workers.UvicornWorker -w 4 geo_web.server:app
CMD ["python", "-m", "geo_web.server"]

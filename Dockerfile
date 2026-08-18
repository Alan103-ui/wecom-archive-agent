# wecom-archive-agent 私有化部署镜像
# 构建：docker build -t wecom-archive-agent:latest .
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# RapidOCR / onnxruntime / Pillow / pymupdf 需要的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libstdc++6 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY scripts ./scripts

# 运行期数据（SQLite/媒体/日志/License/密钥）挂载卷持久化
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8002

# 生产默认强制 License（可在 .env 覆盖为 false）
ENV LICENSE_REQUIRED=true

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]

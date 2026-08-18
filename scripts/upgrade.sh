#!/usr/bin/env bash
# upgrade.sh — 私有化升级（保留数据，可回滚）
# 用法：bash scripts/upgrade.sh [镜像TAG]
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-latest}"
TS=$(date +%Y%m%d-%H%M%S)

echo "==> 1/4 备份数据"
[ -d data ] && tar -czf "data/backup-pre-upgrade-${TS}.tar.gz" -C data . 2>/dev/null || true
echo "备份完成：data/backup-pre-upgrade-${TS}.tar.gz"

echo "==> 2/4 拉取新镜像/构建"
docker compose build --pull 2>/dev/null || docker compose build

echo "==> 3/4 滚动升级"
docker compose up -d

echo "==> 4/4 健康检查"
sleep 5
curl -sf http://127.0.0.1:8002/healthz >/dev/null \
  && echo "升级完成：http://127.0.0.1:8002" \
  || { echo "升级异常，回滚到上一版：docker compose up -d --build（使用备份数据）"; exit 1; }

echo
echo "如需回滚数据：将 data/backup-pre-upgrade-${TS}.tar.gz 解压回 data/ 后重启容器"

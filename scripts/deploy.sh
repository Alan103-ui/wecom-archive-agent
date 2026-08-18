#!/usr/bin/env bash
# deploy.sh — 私有化一键部署（首次部署 / 重建）
# 用法：bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/4 检查环境"
command -v docker >/dev/null || { echo "错误：未安装 Docker，请先安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "错误：需要 Docker Compose v2"; exit 1; }

echo "==> 2/4 生成 .env（如不存在）"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env，请编辑其中的 AUTH_SECRET_KEY / 企业微信凭证 / 数据库等配置后重新执行本脚本"
  echo "配置完成后：bash scripts/deploy.sh"
  exit 0
fi

echo "==> 3/4 启动服务（含 License 校验）"
docker compose up -d --build

echo "==> 4/4 健康检查"
sleep 5
curl -sf http://127.0.0.1:8002/healthz >/dev/null && echo "服务已就绪：http://127.0.0.1:8002" || {
  echo "服务未就绪，查看日志：docker compose logs --tail 50"
  exit 1
}
echo
echo "首次使用：浏览器打开 http://<服务器IP>:8002 ，用默认管理员 admin/admin123 登录后立即改密"

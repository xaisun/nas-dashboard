#!/usr/bin/env bash
# NAS 监控看板 - 一键部署脚本
set -e
cd "$(dirname "$0")"

echo "==> 检查 Docker..."
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 未安装 Docker，请先安装: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "❌ 当前用户无法访问 Docker。请先执行:"
    echo "   sudo usermod -aG docker \$USER && sudo systemctl restart docker"
    echo "   然后重新登录，或用: sudo bash $0"
    exit 1
fi

echo "==> 构建并启动看板 (端口 8904)..."
docker compose up -d --build

echo
echo "✅ 部署完成！"
echo "   访问地址: http://$(hostname -I | awk '{print $1}'):8904"
echo "   查看日志: sudo docker logs -f nas-dashboard"

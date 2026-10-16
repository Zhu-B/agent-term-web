#!/bin/sh
# setup-server.sh —— agent-term web 后端在租来的服务器上一键装成系统服务
# 用法(在 ~/agent-term-web 目录里):
#   sudo sh setup-server.sh 你的访问令牌
# 装完: 开机自启 + 崩了自动拉起, 端口 8787(改 PORT 环境变量重跑).
set -e
[ "$(id -u)" = 0 ] || { echo "要用 sudo 跑"; exit 1; }
TOKEN="${1:?用法: sudo sh setup-server.sh 你的访问令牌}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
[ -x "$PY" ] || { echo "缺 python3 (Ubuntu: apt install -y python3)"; exit 1; }
id -u atweb >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin atweb
mkdir -p /var/lib/atweb
chown atweb:atweb /var/lib/atweb
cat > /etc/systemd/system/atweb.service <<UNIT
[Unit]
Description=agent-term web backend
After=network-online.target

[Service]
User=atweb
WorkingDirectory=$DIR/backend
Environment=PORT=8787
Environment=AT_WEB_TOKEN=$TOKEN
Environment=HOME=/var/lib/atweb
ExecStart=$PY $DIR/backend/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now atweb
sleep 1
systemctl --no-pager -l status atweb | head -8
echo
echo "好了: http://服务器IP:8787  (令牌: $TOKEN)"
echo "注意: 去云控制台防火墙放行 8787/TCP; 改令牌或端口后重跑本脚本."

#!/bin/sh
# agent-term web 后端启动(端口看 PORT 环境变量, 默认 8787; 公网部署设 AT_WEB_TOKEN)
cd "$(dirname "$0")"
exec python3 server.py

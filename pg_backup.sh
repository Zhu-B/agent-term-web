#!/bin/sh
# 每日逻辑备份 atweb PG 库,保留 14 份(口径见 CUTOVER-DB.md / DEPLOY.md)
# 部署: /opt/agent-term-web/pg_backup.sh, ubuntu crontab 每日 4:17
set -e
D=/opt/agent-term-web/data/backup
mkdir -p "$D"
F="$D/atweb-$(date +%Y%m%d-%H%M%S).sql.gz"
sudo -n -u postgres pg_dump --clean --if-exists atweb | gzip > "$F"
ls -1t "$D"/atweb-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

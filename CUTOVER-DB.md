# 数据库切闸手册（SQLite → PostgreSQL，几秒钟的停机窗口）

> 前提: 新代码(webusers 双后端 + AT_DB_URL + data 挪出代码树)已就位;
> **全程不碰运行中的 atweb, 只有第 6 步 restart 那一下**。停机 ≈ 3 秒 +
> 在线用户 SSE 自动重连。
> ⚠️ 时序红线: 本版代码默认读 `../data`(代码树外) —— **挪目录(第3步)必须与
> 新代码同一次重启窗口落地**, 顺序 = 先 mv 再覆盖代码再 restart; 只换代码不
> 挪目录 = 新代码读到空 data → 全员登录失效。

在服务器上按序执行（Ubuntu 24.04, 都用 sudo）：

```bash
# 0) 旁路自检(不碰 /opt 的运行服务): 新代码拷到临时目录, 测试库跑全量自检
cp -r /opt/agent-term-web /tmp/atweb_pg_test
sudo apt install -y postgresql python3-psycopg2
sudo -u postgres psql -c "CREATE ROLE atweb_test LOGIN PASSWORD 't123'"
sudo -u postgres createdb -O atweb_test atweb_test
cd /tmp/atweb_pg_test/backend
AT_DATA=/tmp/atweb_sbx AT_DB_URL=postgres://atweb_test:t123@127.0.0.1:5432/atweb_test \
    python3 server.py --check          # 必须 18 项全绿, 否则停

# 1) 正式库(密码自己换一个强的)
sudo -u postgres psql -c "CREATE ROLE atweb LOGIN PASSWORD '换成强密码'"
sudo -u postgres createdb -O atweb atweb

# 2) PG 只听本机(默认即是, 确认一眼): 配置里 listen_addresses='localhost'

# 3) 迁移现有账号+审计(幂等, 可重跑); 顺带把 data/ 挪出代码树(E1.7:
#    新代码默认读 ../data, 挪完"整目录覆盖 backend/"永远碰不到用户数据)
sudo mv /opt/agent-term-web/backend/data /opt/agent-term-web/data
sudo chown -R atweb:atweb /opt/agent-term-web/data
cd /opt/agent-term-web/backend
AT_DB_URL=postgres://atweb:密码@127.0.0.1:5432/atweb \
    sudo -E python3 migrate_sqlite_to_pg.py
#    (注意用运行用户的家目录上下文; 输出应列出 haotian/zhihua/like)

# 4) systemd unit 加一行环境变量(编辑 /etc/systemd/system/atweb.service):
#    Environment=AT_DB_URL=postgres://atweb:密码@127.0.0.1:5432/atweb
sudo systemctl daemon-reload

# 5) 切闸(唯一停机步, ~3 秒):
sudo systemctl restart atweb

# 6) 验证
curl -s -H "Authorization: Bearer AT_WEB_TOKEN那把" http://127.0.0.1:8787/api/admin/users
#    三人俱在 + 登录照旧 + /api/admin/audit 在涨 → 完成
```

回退方案：把 unit 里那行 AT_DB_URL 删掉 + daemon-reload + restart，即回
sqlite（数据还在 data/webusers.db，切闸期间的新审计会少几条，可接受）。

---

## 接管存档 2026-10-16 晚（原会话 429 上限 20:31 重置，今日先停，恢复按此档）

**本地状态**: 双后端 + 迁移脚本 + 本手册齐备（Windows 主副本 == WSL 副本，已同步）；
sqlite 路 `python server.py --check` 18/18 全绿（接管会话复跑确认）。改动未 commit。

**服务器半途状态**（原会话最后一条命令被 429 掐断，现场未验证；恢复第一步 = 探针复查）:
- ① `apt-get install postgresql python3-psycopg2` 可能已执行;
- ② 新 backend 可能已 scp 到 `/tmp/atweb_pg_test_x` 并展开为 `/tmp/atweb_pg_test`;
- ③ 测试角色/库 `atweb_test`（密码 t123）可能已建。
- 探针点: `psql -V` / `systemctl is-active postgresql` / `ls /tmp/atweb_pg_test/backend` /
  `sudo -u postgres psql -tAc "SELECT rolname FROM pg_roles WHERE rolname='atweb_test'"`
  / `systemctl is-active atweb`。

**登录通道（重要）**: 腾讯云镜像禁 root 直登; 唯一通道 = `ubuntu@<服务器IP>`
密码登录（sshpass 只在 WSL 有）。WSL 的 atweb_key 与新建的 **atweb_pg** 公钥均**未**装进
服务器 authorized_keys——恢复时若想免密，先装 atweb_pg 公钥 + 设 NOPASSWD sudo（引导
命令已备）; 否则照旧 `echo 密码 | sudo -S`。sudo 需密码。

**手册修正（恢复时必须照做）**:
1. 生产用户库在 `backend/data/webusers.db`（E1.3 起 server.py 把 webusers.DB_PATH 指到
   DATA_DIR），挪目录后 = `/opt/agent-term-web/data/webusers.db`。
2. 第3步 migrate **必须显式传参**:
   `python3 migrate_sqlite_to_pg.py /opt/agent-term-web/data/webusers.db`
   —— 脚本默认 src 靠 setdefault(AT_DATA)=backend/data，mv 之后必找不到（报"找不到 sqlite"）。
3. 自检 `build_server(0)` 随机端口，与运行中的 8787 不冲突; 自检自带 AT_DATA 沙箱，第0步
   直接 `AT_DB_URL=postgres://atweb_test:t123@127.0.0.1:5432/atweb_test python3 server.py --check`。
4. 第0步测试树 = /tmp/atweb_pg_test（只有新 backend），无需拷 /opt 整树。

**六步主线不变**: 0 旁路 18 项全绿 → 1 建 atweb 库（强密码）→ 2 确认 PG 只监听本机 →
3 mv data + 覆盖 backend + 迁移（显式路径）→ 4 unit 加
`Environment=AT_DB_URL=postgres://atweb:密码@127.0.0.1:5432/atweb` → 5 restart（~3秒）→
6 验证三人俱在 + 登录 + 审计。回退 = 删 env 行 restart。

---

## 切闸完成 2026-10-19（接管会话执行完毕）

**结果**: atweb 已跑在 PostgreSQL 上; haotian/zhihua/like 三人俱在(admin); 审计写路径已实证
落在 PG(切闸后 401 探针的 login_fail 直接出现在 PG audit 表头); 公网
http://<服务器IP>:8787 正常。

**执行中修的雷**: webusers.py `_sql`/`_exec` 直接 `c.execute(...)`——sqlite 连接自带 execute
所以 sqlite 路 18/18 假绿; psycopg2 连接无此方法, PG 路首个鉴权请求即崩(RemoteDisconnected)。
已修为 PG 走 `c.cursor()`(两副本同步, sqlite 路复验 18/18, 服务器旁路 PG 路 18/18)。

**现场落物**:
- 库密码: /root/.atweb_pg_pass(600, root only)
- sqlite 备份: /opt/agent-term-web/data/webusers.db.pre-pg
- unit 原版: /root/atweb.service.pre-pg
- 免密通道: WSL ~/.ssh/atweb_pg 公钥已装服务器; ubuntu NOPASSWD sudo(/etc/sudoers.d/atweb-claude, 不需要可删)

**回退**: `cp /root/atweb.service.pre-pg /etc/systemd/system/atweb.service && systemctl daemon-reload && systemctl restart atweb`
—— 回 sqlite(数据在 /opt/agent-term-web/data/webusers.db, 切闸后的新审计在 PG 里)。

**后续待办**: ①pg_dump 定时备份(口径: data/backup 留 14 份+异地加密); ②三个 admin 考虑降权(用户此前提过);
③工作区改动(双后端+修复+手册)待 commit; ④测试库 atweb_test 可留可删。

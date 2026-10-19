# -*- coding: utf-8 -*-
"""migrate_sqlite_to_pg.py — 一次性/幂等: sqlite(data/webusers.db) → PostgreSQL.

用法(在 backend/ 下, AT_DB_URL 指向目标 PG):
  AT_DB_URL=postgres://atweb:密码@127.0.0.1:5432/atweb \
      python3 migrate_sqlite_to_pg.py [sqlite路径, 默认 data/webusers.db]

幂等规则:
- users: 按 uid 存在即跳过(PG 侧的手工改动不被覆盖)
- audit: 按 max(ts) 增量补(每次切闸前重跑一遍, 把停机窗口里漏掉的尾巴带上)
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AT_DATA", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"))
import server                      # noqa: E402  (AT_DATA 归位 + core 挂载)
import webusers                    # noqa: E402

if not webusers.PG_MODE:
    print("先设 AT_DB_URL=postgres://… 再跑本脚本")
    raise SystemExit(1)

src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    server.DATA_DIR, "webusers.db")
if not os.path.isfile(src):
    print("找不到 sqlite: %s" % src)
    raise SystemExit(1)

lite = sqlite3.connect(src)
lite.row_factory = sqlite3.Row
users = [dict(r) for r in lite.execute(
    "SELECT uid,username,pass_hash,role,token,disabled,created FROM users")]
max_ts = 0.0
for r in lite.execute("SELECT ts FROM audit ORDER BY id DESC LIMIT 1"):
    max_ts = float(r["ts"] or 0)
audit_rows = [dict(r) for r in lite.execute(
    "SELECT ts,actor,action,detail FROM audit WHERE ts > ? - 1", (max_ts,))]

n_u = n_a = 0
for u in users:
    with webusers._db() as c:
        if webusers._sql(c, "SELECT 1 x FROM users WHERE uid=?", (u["uid"],)):
            continue
        webusers._sql(c, "INSERT INTO users(uid,username,pass_hash,role,"
                         "token,disabled,created) VALUES(?,?,?,?,?,?,?)",
                      (u["uid"], u["username"], u["pass_hash"], u["role"],
                       u["token"], u["disabled"], u["created"]))
        n_u += 1
for a in audit_rows:
    webusers.audit(a["actor"], a["action"], a["detail"] or "")
    n_a += 1
with webusers._db() as c:
    total = len(webusers._sql(c, "SELECT uid,username,role FROM users"))
print("迁移完成: 新增用户 %d / 补审计 %d / PG 现有用户 %d" % (n_u, n_a, total))
for r in webusers.list_users():
    print("  -", r["username"], r["role"])

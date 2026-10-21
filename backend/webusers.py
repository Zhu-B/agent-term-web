# -*- coding: utf-8 -*-
"""webusers.py — E1 多用户地基: 账号/角色/令牌/审计.

双后端(E1.6): 默认 **sqlite**(纯标准库, data/webusers.db, 本地开发/自检);
设 AT_DB_URL=postgres://user:pass@127.0.0.1:5432/dbname 则走 **PostgreSQL**
(生产; 依赖 python3-psycopg2, 惰性 import —— sqlite 路径保持零 pip 依赖).
上层 API 完全一致; SQL 统一写 "?" 占位, PG 分支自动翻成 %s.

口径:
- 超管开户制, 无开放注册; 用户表空且没设 AT_WEB_TOKEN = 开放模式(本机开发/
  自检, 不鉴权), 其余情况一律要令牌.
- AT_WEB_TOKEN = 超管影子令牌(引导口).
- 密码只存 pbkdf2 哈希(salt$hex); 令牌 = "u-" + 32 hex.
- 审计表只增不改 —— "全员可切版本"这类全局操作的底线安全网.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.expanduser("~"), ".agent-term", "webusers.db")
DB_URL = os.environ.get("AT_DB_URL", "").strip()      # 空=sqlite; postgres://…=PG
PG_MODE = DB_URL.startswith("postgres")
_lock = threading.Lock()

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users(
  uid TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  pass_hash TEXT DEFAULT '', role TEXT DEFAULT 'user',
  token TEXT UNIQUE, disabled INTEGER DEFAULT 0,
  created REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT,
  action TEXT, detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS versions(          -- E2: 版本登记(快照文件在
  vid TEXT PRIMARY KEY, label TEXT DEFAULT '', -- versions/<vid>/, 树外)
  note TEXT DEFAULT '', created_by TEXT DEFAULT '',
  created_at REAL DEFAULT 0, file_count INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0, trashed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects(          -- E6: 项目(会话+忆库+账本的容器)
  pid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '',
  owner TEXT DEFAULT '', created_at REAL DEFAULT 0, public INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_shares(    -- E6: 协作(perm: read 只读/write 可编辑)
  pid INTEGER NOT NULL, uid TEXT NOT NULL, perm TEXT DEFAULT 'read',
  created_at REAL DEFAULT 0, PRIMARY KEY(pid, uid));
"""
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users(
  uid TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  pass_hash TEXT DEFAULT '', role TEXT DEFAULT 'user',
  token TEXT UNIQUE, disabled INTEGER DEFAULT 0,
  created REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id BIGSERIAL PRIMARY KEY, ts REAL, actor TEXT,
  action TEXT, detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS versions(
  vid TEXT PRIMARY KEY, label TEXT DEFAULT '',
  note TEXT DEFAULT '', created_by TEXT DEFAULT '',
  created_at REAL DEFAULT 0, file_count INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0, trashed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects(
  pid BIGSERIAL PRIMARY KEY, name TEXT DEFAULT '',
  owner TEXT DEFAULT '', created_at REAL DEFAULT 0, public INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_shares(
  pid INTEGER NOT NULL, uid TEXT NOT NULL, perm TEXT DEFAULT 'read',
  created_at REAL DEFAULT 0, PRIMARY KEY(pid, uid));
"""
_schema_ready = False


def _connect():
    if not PG_MODE:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row      # dict(r) 能吃的前提
        return conn
    import psycopg2
    import psycopg2.extras
    u = psycopg2.extras
    import urllib.parse as _up
    p = _up.urlparse(DB_URL)
    return psycopg2.connect(
        dbname=p.path.lstrip("/"), user=p.username,
        password=_up.unquote(p.password or ""), host=p.hostname,
        port=p.port or 5432,
        cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def _db():
    """连接上下文: 成功提交, 异常回滚, 用完关死(两种后端同语义)."""
    global _schema_ready
    conn = _connect()
    try:
        if not _schema_ready:
            if PG_MODE:
                conn.cursor().execute(SCHEMA_PG)      # PG 一次 execute 吃多句
                conn.cursor().execute(                # 老库加列(2026-10-20)
                    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS"
                    " public INTEGER DEFAULT 0")
            else:
                conn.executescript(SCHEMA_SQLITE)     # sqlite 要 executescript
                try:                                  # sqlite 无 IF NOT EXISTS,
                    conn.execute("ALTER TABLE projects ADD COLUMN"
                                 " public INTEGER DEFAULT 0")
                except Exception:
                    pass
            _schema_ready = True
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sql(c, q, params=()):
    """统一执行: SQL 写 "?", PG 分支翻成 %s. 返回 dict 行列表."""
    if PG_MODE:
        q = q.replace("?", "%s")
    if PG_MODE:
        cur = c.cursor()                          # psycopg2 连接无 execute
        cur.execute(q, params)
    else:
        cur = c.execute(q, params)                # sqlite 连接自带 execute
    try:
        rows = cur.fetchall()
    except Exception:
        rows = []
    if rows and not isinstance(rows[0], dict):      # sqlite Row → dict
        rows = [dict(r) for r in rows]
    return rows


def _exec(c, q, params=()):
    """写语句(UPDATE/DELETE) → 受影响行数(两后端都有 rowcount)."""
    if PG_MODE:
        q = q.replace("?", "%s")
        cur = c.cursor()
        cur.execute(q, params)
    else:
        cur = c.execute(q, params)
    return cur.rowcount or 0


def count() -> int:
    with _lock, _db() as c:
        return int((_sql(c, "SELECT COUNT(*) n FROM users") or [{"n": 0}])[0]
                   .get("n") or 0)


# ---------------- 密码 ----------------
def _hash(password: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            salt.encode("utf-8"), 100_000)
    return salt + "$" + h.hex()


def _check(password: str, stored: str) -> bool:
    try:
        salt, want = stored.split("$", 1)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                              salt.encode("utf-8"), 100_000).hex()
    return hmac.compare_digest(got, want)


# ---------------- 账号操作(超管) ----------------
def create_user(username, password, role="user") -> tuple:
    username = (username or "").strip()
    if not username or len(username) > 32 or "/" in username:
        return False, "用户名要 1~32 位且不含 /"
    if role not in ("user", "admin"):
        return False, "role 只认 user/admin"
    if not (password or "").strip():
        return False, "密码不能为空"
    uid = secrets.token_hex(4)
    token = "u-" + secrets.token_hex(16)
    try:
        with _lock, _db() as c:
            if _sql(c, "SELECT 1 x FROM users WHERE username=?", (username,)):
                return False, "用户名已存在"
            _sql(c, "INSERT INTO users(uid,username,pass_hash,role,token,"
                    "created) VALUES(?,?,?,?,?,?)",
                 (uid, username, _hash(password), role, token, time.time()))
    except Exception as ex:
        return False, "写入失败: %s" % ex
    audit("admin", "user_create", "%s(%s)" % (username, role))
    return True, {"uid": uid, "username": username, "role": role,
                  "token": token}


def set_disabled(username, on: bool) -> tuple:
    with _lock, _db() as c:
        n = _exec(c, "UPDATE users SET disabled=? WHERE username=?",
                  (1 if on else 0, username))
    if not n:
        return False, "没有这个用户"
    audit("admin", "user_disabled" if on else "user_enabled", username)
    return True, username


def reset_password(username, password) -> tuple:
    if not (password or "").strip():
        return False, "密码不能为空"
    with _lock, _db() as c:
        n = _exec(c, "UPDATE users SET pass_hash=? WHERE username=?",
                  (_hash(password), username))
    if not n:
        return False, "没有这个用户"
    audit("admin", "user_password", username)
    return True, username


def reset_token(username) -> tuple:
    token = "u-" + secrets.token_hex(16)
    with _lock, _db() as c:
        n = _exec(c, "UPDATE users SET token=? WHERE username=?",
                  (token, username))
    if not n:
        return False, "没有这个用户"
    audit("admin", "user_token", username)
    return True, {"username": username, "token": token}


def list_users() -> list:
    with _lock, _db() as c:
        return _sql(c, "SELECT uid,username,role,disabled,created FROM users"
                    " ORDER BY created")


# ---------------- 登录与鉴权 ----------------
def login(username, password) -> tuple:
    with _lock, _db() as c:
        rows = _sql(c, "SELECT * FROM users WHERE username=?", (username or "",))
    r = rows[0] if rows else None
    if r is None or r["disabled"] or not r["pass_hash"] \
            or not _check(password or "", r["pass_hash"]):
        audit(username or "?", "login_fail", "")
        return False, "用户名或密码不对(或被停用)"
    audit(username, "login_ok", "")
    return True, {"uid": r["uid"], "username": r["username"],
                  "role": r["role"], "token": r["token"]}


OPEN_USER = {"uid": None, "username": "open", "role": "admin"}   # 开放模式影子
ADMIN_SHADOW = {"uid": None, "username": "admin", "role": "admin"}


def auth(token, master_token="") -> dict:
    """令牌 → 用户行; 无效返 None.
    开放模式(没设主令牌且用户表空) = 谁都是超管(仅限本机开发/自检)."""
    if not master_token and count() == 0:
        return OPEN_USER
    if master_token and token and hmac.compare_digest(token, master_token):
        return ADMIN_SHADOW                    # 超管影子令牌
    if not token:
        return None
    with _lock, _db() as c:
        rows = _sql(c, "SELECT * FROM users WHERE token=?", (token,))
    r = rows[0] if rows else None
    if r is None or r["disabled"]:
        return None
    return {"uid": r["uid"], "username": r["username"], "role": r["role"],
            "token": r["token"]}


# ---------------- 审计 ----------------
def audit(actor, action, detail=""):
    try:
        with _lock, _db() as c:
            _sql(c, "INSERT INTO audit(ts,actor,action,detail) "
                    "VALUES(?,?,?,?)",
                 (time.time(), str(actor)[:64], str(action)[:64],
                  str(detail)[:500]))
    except Exception:
        pass                       # 审计崩了不伤业务, 但不该发生


def list_audit(limit=100) -> list:
    with _lock, _db() as c:
        return _sql(c, "SELECT ts,actor,action,detail FROM audit"
                    " ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit or 100), 1000)),))


# ---------------- E6 项目与分享(2026-10-20) ----------------
def create_project(name, owner, public=False):
    """建项目 → dict 行(pid,name,owner,created_at,public). 失败 None.
    public=True = 公共项目(所有人都能看到, 非项目主只读)."""
    with _lock, _db() as c:
        _sql(c, "INSERT INTO projects(name,owner,created_at,public)"
                " VALUES(?,?,?,?)",
             (str(name or "")[:80], str(owner or ""), time.time(),
              1 if public else 0))
        rows = _sql(c, "SELECT pid,name,owner,created_at,public FROM projects"
                       " WHERE owner=? ORDER BY pid DESC LIMIT 1",
                    (str(owner or ""),))
    return rows[0] if rows else None


def set_project_public(pid, on):
    """公共项目开关(owner 用)."""
    with _lock, _db() as c:
        _exec(c, "UPDATE projects SET public=? WHERE pid=?",
              (1 if on else 0, int(pid)))


def list_public_projects(uid):
    """公共项目(所有人可见; 不含自己的 —— 自己的在 list_own_projects 里)."""
    with _lock, _db() as c:
        return _sql(c, "SELECT p.pid,p.name,p.owner,p.created_at,p.public,"
                       "u.username AS owner_name FROM projects p"
                       " LEFT JOIN users u ON u.uid=p.owner"
                       " WHERE p.public=1 AND p.owner!=? ORDER BY p.pid",
                    (str(uid or ""),))


def get_project(pid):
    with _lock, _db() as c:
        rows = _sql(c, "SELECT pid,name,owner,created_at,public FROM projects"
                       " WHERE pid=?", (int(pid),))
    return rows[0] if rows else None


def rename_project(pid, name):
    with _lock, _db() as c:
        _exec(c, "UPDATE projects SET name=? WHERE pid=?", (str(name)[:80], int(pid)))
    return True


def list_own_projects(uid):
    with _lock, _db() as c:
        return _sql(c, "SELECT pid,name,owner,created_at,public FROM projects"
                       " WHERE owner=? ORDER BY pid", (str(uid or ""),))


def list_shared_projects(uid):
    """被分享给我的项目(带 owner 名与我的权限)."""
    with _lock, _db() as c:
        return _sql(c, "SELECT p.pid,p.name,p.owner,p.created_at,s.perm,"
                       "u.username AS owner_name FROM project_shares s"
                       " JOIN projects p ON p.pid=s.pid"
                       " LEFT JOIN users u ON u.uid=p.owner"
                       " WHERE s.uid=? ORDER BY p.pid", (str(uid or ""),))


def shares_of(pid):
    """这个项目分享给了谁(带用户名)."""
    with _lock, _db() as c:
        return _sql(c, "SELECT s.uid,s.perm,s.created_at,u.username"
                       " FROM project_shares s LEFT JOIN users u ON u.uid=s.uid"
                       " WHERE s.pid=? ORDER BY s.created_at", (int(pid),))


def upsert_share(pid, uid, perm):
    """分享/改权限(perm: read|write). UPDATE 优先, 无行再 INSERT(撞主键不炸)."""
    with _lock, _db() as c:
        n = _exec(c, "UPDATE project_shares SET perm=? WHERE pid=? AND uid=?",
                  (str(perm), int(pid), str(uid)))
        if not n:
            _sql(c, "INSERT INTO project_shares(pid,uid,perm,created_at)"
                    " VALUES(?,?,?,?)",
                 (int(pid), str(uid), str(perm), time.time()))


def remove_share(pid, uid):
    with _lock, _db() as c:
        _exec(c, "DELETE FROM project_shares WHERE pid=? AND uid=?",
              (int(pid), str(uid)))


def project_access(pid, uid):
    """该用户对项目的权限: "owner" | "write" | "read" | ""(不可见).
    公共项目(public=1): 登录者全员可见(非项目主只读)."""
    p = get_project(pid)
    if p is None:
        return ""
    if p["owner"] == str(uid or ""):
        return "owner"
    if p.get("public"):
        return "read"
    with _lock, _db() as c:
        rows = _sql(c, "SELECT perm FROM project_shares"
                       " WHERE pid=? AND uid=?", (int(pid), str(uid or "")))
    return (rows[0].get("perm") or "read") if rows else ""


def default_project(uid):
    """用户默认个人项目(没有就建「我的项目」)."""
    rows = list_own_projects(uid)
    if rows:
        return rows[0]
    return create_project("我的项目", uid)

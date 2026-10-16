# -*- coding: utf-8 -*-
"""webusers.py — E1 多用户地基: 账号/角色/令牌/审计(sqlite, 纯标准库).

口径:
- 超管开户制, 无开放注册; 用户表空且没设 AT_WEB_TOKEN = 开放模式(本机开发/
  自检, 不鉴权), 其余情况一律要令牌.
- AT_WEB_TOKEN = 超管影子令牌(谁拿着它谁就是 admin, 不在用户表里; 适合当
  引导口: 装好后直接用它登录开第一批账号, 也可以给 admin 设密码后换常规登录).
- 密码只存 pbkdf2 哈希(salt$hex, 十万轮); 令牌 = "u-" + 32 hex.
- 审计表只增不改(谁在什么时候干了什么), 是"全员可切版本"这类全局操作的
  底线安全网.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.expanduser("~"), ".agent-term", "webusers.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  uid TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  pass_hash TEXT DEFAULT '', role TEXT DEFAULT 'user',
  token TEXT UNIQUE, disabled INTEGER DEFAULT 0,
  created REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT,
  action TEXT, detail TEXT DEFAULT '');
"""


_schema_ready = False


def _db():
    global _schema_ready
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _schema_ready:                 # 懒建表: 自检先把 DB_PATH 指沙箱
        conn.executescript(SCHEMA)
        _schema_ready = True
    return conn


def ensure_schema():
    with _lock, _db() as c:
        c.executescript(SCHEMA)


def count() -> int:
    with _lock, _db() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


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
            c.execute("INSERT INTO users(uid,username,pass_hash,role,token,"
                      "created) VALUES(?,?,?,?,?,?)",
                      (uid, username, _hash(password), role, token,
                       time.time()))
    except sqlite3.IntegrityError:
        return False, "用户名已存在"
    audit("admin", "user_create", "%s(%s)" % (username, role))
    return True, {"uid": uid, "username": username, "role": role,
                  "token": token}


def set_disabled(username, on: bool) -> tuple:
    with _lock, _db() as c:
        cur = c.execute("UPDATE users SET disabled=? WHERE username=?",
                        (1 if on else 0, username))
        if cur.rowcount == 0:
            return False, "没有这个用户"
    audit("admin", "user_disabled" if on else "user_enabled", username)
    return True, username


def reset_password(username, password) -> tuple:
    if not (password or "").strip():
        return False, "密码不能为空"
    with _lock, _db() as c:
        cur = c.execute("UPDATE users SET pass_hash=? WHERE username=?",
                        (_hash(password), username))
        if cur.rowcount == 0:
            return False, "没有这个用户"
    audit("admin", "user_password", username)
    return True, username


def reset_token(username) -> tuple:
    token = "u-" + secrets.token_hex(16)
    with _lock, _db() as c:
        cur = c.execute("UPDATE users SET token=? WHERE username=?",
                        (token, username))
        if cur.rowcount == 0:
            return False, "没有这个用户"
    audit("admin", "user_token", username)
    return True, {"username": username, "token": token}


def list_users() -> list:
    with _lock, _db() as c:
        rows = c.execute("SELECT uid,username,role,disabled,created FROM users"
                         " ORDER BY created").fetchall()
    return [dict(r) for r in rows]


# ---------------- 登录与鉴权 ----------------
def login(username, password) -> tuple:
    with _lock, _db() as c:
        r = c.execute("SELECT * FROM users WHERE username=?",
                      (username or "",)).fetchone()
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
        r = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if r is None or r["disabled"]:
        return None
    return {"uid": r["uid"], "username": r["username"], "role": r["role"],
            "token": r["token"]}


# ---------------- 审计 ----------------
def audit(actor, action, detail=""):
    try:
        with _lock, _db() as c:
            c.execute("INSERT INTO audit(ts,actor,action,detail) "
                      "VALUES(?,?,?,?)",
                      (time.time(), str(actor)[:64], str(action)[:64],
                       str(detail)[:500]))
    except Exception:
        pass                       # 审计崩了不伤业务, 但不该发生


def list_audit(limit=100) -> list:
    with _lock, _db() as c:
        rows = c.execute("SELECT ts,actor,action,detail FROM audit"
                         " ORDER BY id DESC LIMIT ?",
                         (max(1, min(int(limit or 100), 1000)),)).fetchall()
    return [dict(r) for r in rows]

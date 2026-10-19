# -*- coding: utf-8 -*-
"""server.py — agent-term Web 后端(纯标准库, 零 pip 依赖).

定位: 在无头核心(core/ 快照, 全部零 tkinter/零 win32)外面包一层 HTTP/SSE 壳 ——
driver.py 头注释"将来服务化是在这层外面再包壳"的兑现. 事件总线是唯一的过程
观测面: 本层给每个会话挂第四个 sink, 把事件投影(project_web, 桌面版 frontend.py
project() 的转写 + perm_ask/ask_user 两张卡)缓冲并推给 SSE.

端点(全 JSON, CORS 全开; AT_WEB_TOKEN 设置后要 Bearer/`?token=`):
  GET  /api/health                     存活+模型清单(demo 标记)
  GET  /api/sessions                   本进程内的会话列表
  POST /api/sessions      {model}      新建会话 → {sid}
  GET  /api/sessions/{sid}/messages    历史回放(重启前的兜底, 平铺气泡)
  GET  /api/sessions/{sid}/events?since=seq   SSE: 先补放缓冲再 live(15s 心跳)
  POST /api/sessions/{sid}/send        {text} 后台跑一轮, 过程走 SSE
  POST /api/sessions/{sid}/interrupt   中断当前轮
  POST /api/sessions/{sid}/ask/{id}/answer   答 perm_ask(allow/always/deny) 或
                                             ask_user({label,custom,skipped})
  DELETE /api/sessions/{sid}           收尾销会话
  GET/POST /api/models                 模型清单 / 写自增档(key 只落盘, 回显恒掩码)

静态托管: 默认 ../frontend 存在则挂在 /(同源部署免 CORS); AT_WEB_STATIC 可改.
演示模式: AT_DEMO=1 无 key 也可跑(剧本 provider, 一次工具调用+一次结论循环).
自检: python server.py --check (selfcheck.py 全链路, 不联网).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "core")

# E1.3 状态收拢: 一切服务端状态进显式 data/ 目录(AT_DATA 可指到别处, 如挂持久盘).
# 必须在 import core 之前设 env —— core 的模块常量在导入时读 env.
# 此前家法是散在 ~/.agent-term 发现链: 跑在哪台机器就静默继承哪台机器的配置
# (本地调试会把开发者自己的 models/keys 读出来), 自包含部署从根上灭掉这类事.
# E1.7: data 挪到代码树**外面**(backend 的上一级) —— E2 切版本要整目录覆盖
# backend/, 数据目录长在树内迟早被埋; 树外后"覆盖代码"与"数据"物理绝缘.
DATA_DIR = os.environ.get("AT_DATA") or os.path.normpath(
    os.path.join(HERE, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("AT_PROVIDERS", os.path.join(DATA_DIR, "providers.json"))
os.environ["AT_SETTINGS"] = os.path.join(DATA_DIR, "settings.json")
os.environ["AT_MEMORY_DIR"] = os.path.join(DATA_DIR, "memory")
os.environ["AT_LEDGER_DIR"] = os.path.join(DATA_DIR, "ledger")
os.environ["AT_SKILLS_DIR"] = os.path.join(DATA_DIR, "skills-user")
os.environ["AT_SCHED_FILE"] = os.path.join(DATA_DIR, "schedule.json")
os.environ["AT_MEDIA_DIR"] = os.path.join(DATA_DIR, "media")

sys.path.insert(0, CORE)

import agent                      # noqa: E402  (无头核心快照)
import driver
import instr
import ledger as ledger_mod
import memory as memory_mod
import perms as perms_mod
import provider as provider_mod
import prompts as prompts_mod
import session as session_mod
import skills as skills_mod
import tools
import webusers
from provider import ChatResult   # noqa: E402

os.chdir(HERE)                    # run/sessions、run/trace 落在 backend/ 下

TOKEN = os.environ.get("AT_WEB_TOKEN", "").strip()
DEMO = os.environ.get("AT_DEMO") == "1"
STATIC_DIR = os.environ.get("AT_WEB_STATIC") or os.path.join(HERE, "..", "frontend")
STATIC_DIR = os.path.normpath(STATIC_DIR) if os.path.isdir(STATIC_DIR) else None
MAX_SESSIONS = 20                 # 免费小内存主机的护栏(最闲的先逐出)
ASK_POLL = 0.2                    # ask 阻塞轮询步长(桌面版同款)

# ================== E1 多用户: 线程上下文 + core 分域补丁 ==================
# 每用户数据根: data/users/<uid>/{memory, ledger, skills, models.json, keys.json}
USER_ROOT = os.path.join(DATA_DIR, "users")
provider_mod.KEY_STORE = os.path.join(DATA_DIR, "keys.json")      # 无 env 口子的三个
provider_mod.MODELS_STORE = os.path.join(DATA_DIR, "models.json") # 模块常量, 导入后
provider_mod.SETTINGS_STORE = os.path.join(DATA_DIR, "settings.json")  # 改属性
webusers.DB_PATH = os.path.join(DATA_DIR, "webusers.db")
_CTX = threading.local()          # 当前线程归属的用户(uid None = 超管/公共视角)


def set_user(uid):
    _CTX.uid = uid


def cur_uid():
    return getattr(_CTX, "uid", None)


# 分域手法 = 替换 core 的路径解析函数(按线程查 uid)。不能翻模块常量:
# 多用户并发线程下翻常量会串用户(甲的请求把乙的目录指过来)。
_orig_zone_dir = memory_mod._zone_dir


def _zone_dir(zone="archive"):
    uid = cur_uid()
    if uid is None:
        return _orig_zone_dir(zone)
    return os.path.join(USER_ROOT, uid, "memory", zone)


memory_mod._zone_dir = _zone_dir

_orig_ledger_path = ledger_mod._path


def _ledger_path():
    uid = cur_uid()
    if uid is None:
        return _orig_ledger_path()
    return os.path.join(USER_ROOT, uid, "ledger", "ledger.jsonl")


ledger_mod._path = _ledger_path

_orig_layer_dir = skills_mod._layer_dir


def _layer_dir(source):
    if source == "user":
        uid = cur_uid()
        if uid is not None:
            return os.path.join(USER_ROOT, uid, "skills")
    return _orig_layer_dir(source)


skills_mod._layer_dir = _layer_dir

# 模型档: 公共档(全局文件, 超管维护) + 私有档(用户目录) 合并读, 私有同名盖公共;
# 写按身份路由 —— 超管/开放模式写公共档, 普通用户只写自己的私有档。
# custom_providers() 内部调 load_models(), 换这一个它自动跟着分域。
_orig_load_models = provider_mod.load_models
_orig_save_models = provider_mod.save_models


def _load_models():
    pub = _orig_load_models()
    uid = cur_uid()
    if uid is None:
        return pub
    pp = os.path.join(USER_ROOT, uid, "models.json")
    priv = []
    if os.path.isfile(pp):
        try:
            with open(pp, encoding="utf-8") as f:
                priv = [m for m in json.load(f).get("models", [])
                        if isinstance(m, dict)]
        except Exception:
            priv = []
    priv_labels = {m.get("label") for m in priv
                   if m.get("provider") == "custom"}
    priv_fams = {(m.get("provider"), m.get("model")) for m in priv
                 if m.get("provider") != "custom"}
    out = [m for m in pub if not (
        (m.get("provider") == "custom" and m.get("label") in priv_labels)
        or (m.get("provider") != "custom"
            and (m.get("provider"), m.get("model")) in priv_fams))]
    return out + priv


def _save_models(models):
    uid = cur_uid()
    if uid is None:                       # 超管/开放: 公共档
        _orig_save_models(models)
        return
    pp = os.path.join(USER_ROOT, uid, "models.json")
    pub = _orig_load_models()
    pub_cus = {(m.get("label")) for m in pub
               if m.get("provider") == "custom"}
    pub_fams = {(m.get("provider"), m.get("model")) for m in pub
                if m.get("provider") != "custom"}
    keep = []
    for m in models:                      # 私有档只存"非公共已有"的条目
        if m.get("provider") == "custom":
            if m.get("label") not in pub_cus:
                keep.append(m)
        elif (m.get("provider"), m.get("model")) not in pub_fams:
            keep.append(m)
    os.makedirs(os.path.dirname(pp), exist_ok=True)
    with open(pp, "w", encoding="utf-8") as f:
        json.dump({"models": keep}, f, ensure_ascii=False, indent=2)


provider_mod.load_models = _load_models
provider_mod.save_models = _save_models


# ================================ 演示 provider ================================
class DemoProvider(provider_mod.Provider):
    """AT_DEMO=1 的剧本 provider: 每轮 = 一次 read 工具调用 + 一段结论, 循环播放.
    让部署演示/自检不依赖任何 key."""

    def __init__(self):
        super().__init__("demo", {"base_url": "", "key_env": "AT_FAKE_KEY",
                                  "model": "demo-echo", "family": "test"})
        self.available = True
        self.key = "demo"
        self._i = 0

    def build_payload(self, messages, tools=None, **extra):
        return {"model": self.model, "messages": messages}

    def send(self, payload, on_chunk=None):
        script = [
            {"content": "", "tool_calls": [{
                "id": "d%d" % self._i, "type": "function",
                "function": {"name": "read",
                             "arguments": json.dumps({"path": "README.md",
                                                      "limit": 5})}}]},
            {"content": "(演示回复) 我刚用 read 工具看了一眼 README 的开头 —— "
                        "这是 AT_DEMO 演示模式: 没配任何 key 也能走完"
                        "「工具调用→结果→结论」的完整回路. 要接真模型, 去「模型」"
                        "页加一条自增档(base_url/key/model)."},
        ]
        msg = dict(script[self._i % len(script)])
        self._i += 1
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        return ChatResult(message={"role": "assistant", **msg}, usage=usage,
                          finish_reason=finish, latency_ms=3,
                          raw={"choices": [{"message": msg, "finish_reason": finish}],
                               "usage": usage})


# ================================ 模型目录与解析 ================================
def _mask(key):
    return (key[:3] + "***" + key[-3:]) if key and len(key) > 8 else ("***" if key else "")


def _priv_keys_path(uid):
    return os.path.join(USER_ROOT, uid or "", "keys.json")


def _private_key(uid, family):
    """用户私有家 key(E1: 每家一把). 没有返 ''."""
    try:
        with open(_priv_keys_path(uid), encoding="utf-8") as f:
            return str(json.load(f).get(family) or "")
    except Exception:
        return ""


def _save_private_key(uid, family, key):
    pp = _priv_keys_path(uid)
    os.makedirs(os.path.dirname(pp), exist_ok=True)
    data = {}
    if os.path.isfile(pp):
        try:
            with open(pp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if key:
        data[family] = key
    else:
        data.pop(family, None)
    with open(pp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _family_available(p, uid):
    """官方家可用判定: 公共 key(全局库) 或 本用户私有 key."""
    if p.available:
        return True, "公共"
    if uid is not None:
        k = _private_key(uid, p.name)
        if k:
            return True, "私有"
    return False, ""


def model_catalog():
    """[{label, model, available, kind, base_url, key_mask}] —— 只列**现在就能
    调**的(可用=有公共或本用户私有 key); 缺 key 的家待在「官方家」卡片区, 不进
    这份清单碍眼. key 永远只出掩码."""
    out = []
    if DEMO:
        out.append({"label": "demo/demo-echo", "model": "demo-echo",
                    "available": True, "kind": "demo", "base_url": "",
                    "key_mask": "demo"})
    uid = cur_uid()
    try:
        provs = provider_mod.load_providers()
    except Exception:
        provs = {}
    customs = {m.get("label") for m in provider_mod.load_models()
               if m.get("provider") == "custom"}
    for name, p in provs.items():
        if name in customs:               # 自增档: Provider 名就是条目 label
            if p.available:
                out.append({"label": name, "model": p.model, "available": True,
                            "kind": "custom", "base_url": p.base_url,
                            "key_mask": _mask(p.key)})
            continue
        ok, src = _family_available(p, uid)   # 官方家: 默认档 + 家内档
        if not ok:
            continue                      # 缺 key 的家只在官方家卡片区露脸
        base = {"available": True, "kind": "family", "base_url": p.base_url,
                "key_mask": _mask(p.key if src == "公共"
                                  else _private_key(uid, name)),
                "key_src": src}
        out.append(dict(base, label="%s/%s" % (name, p.model), model=p.model))
        for m in provider_mod.load_models():  # 家内档(精确型号, 用户档合并版)
            if m.get("provider") == name and m.get("model") != p.model:
                out.append(dict(base, label="%s/%s" % (name, m["model"]),
                                model=m["model"]))
    return out


def resolve_model(label):
    """label → 可发请求的 Provider 副本. 自增档/裸家名走名册; '家/型号' 精确
    覆盖型号; 官方家没公共 key 时试本用户私有 key. 找不到/不可用返 None."""
    if DEMO and label in ("demo/demo-echo", "demo", ""):
        return DemoProvider()
    provs = provider_mod.load_providers()
    p = provs.get(label)
    if p is None and "/" in label:
        name, _, model = label.partition("/")
        base = provs.get(name)
        if base is not None:
            p = copy.copy(base)
            p.model = model or p.model
    if p is None:
        return None
    p = copy.copy(p)                       # 不动名册原件
    uid = cur_uid()
    if not p.available and uid is not None:
        k = _private_key(uid, p.name)
        if k:
            p.key, p.available, p.key_from_store = k, True, True
    return p if p.available else None


# ================================ 事件投影(SSE 形) ================================
def project_web(ev):
    """桌面版 frontend.project() 的转写(Web 多两张卡走独立通道, 见 Sess.ask_*).
    返回 None = 不进前端. 追加 seq 由 Sess._sink 统一盖."""
    t, d = ev.get("type"), ev.get("data") or {}
    if t == "_card":                      # 问卡(perm_ask/ask_user/ask_done/note):
        d = dict(d)                       # kind 提为 type, 前端统一按 type 分发
        d["type"] = d.pop("kind")
        return d
    if d.get("internal"):
        return None
    if d.get("sub"):
        sid, lb = d.get("sub"), d.get("sub_label") or d.get("sub")
        if t == "llm_request":
            return {"type": "sub_req", "sid": sid, "label": lb, "model": d.get("model")}
        if t == "tool_call":
            return {"type": "sub_tool_call", "sid": sid, "label": lb,
                    "name": d.get("name"), "args": (d.get("args_raw") or "")[:120]}
        if t == "tool_result":
            return {"type": "sub_tool_result", "sid": sid, "label": lb,
                    "ok": bool(d.get("ok")), "ms": d.get("duration_ms"),
                    "content": (d.get("_content") or "")[:200]}
        if t == "turn_end":
            return {"type": "sub_done", "sid": sid, "label": lb,
                    "iters": d.get("iterations"), "aborted": bool(d.get("aborted"))}
        return None
    if t == "llm_request":
        return {"type": t, "model": d.get("model")}
    if t == "llm_chunk":
        return {"type": t, "kind": d.get("kind"), "text": d.get("text") or ""}
    if t == "turn_start":
        return {"type": t, "text": d.get("user_text", "")}
    if t == "llm_response":
        msg = d.get("message") or {}
        return {"type": t, "text": msg.get("content") or "",
                "tools": bool(msg.get("tool_calls")), "usage": d.get("usage") or {}}
    if t == "tool_call":
        return {"type": t, "name": d.get("name"), "args": (d.get("args_raw") or "")[:200]}
    if t == "tool_result":
        p = {"type": t, "ok": bool(d.get("ok")),
             "content": (d.get("_content") or "")[:1200], "ms": d.get("duration_ms")}
        if d.get("images"):
            p["images"] = [{"path": r.get("path"), "sha1": r.get("sha1"),
                            "bytes": r.get("bytes")} for r in d["images"][:4]]
        return p
    if t == "perm_decision":
        return {"type": t, "name": d.get("name"), "decision": d.get("decision")}
    if t == "todo_update":
        return {"type": t, "items": d.get("items") or []}
    if t == "memory_read":
        return {"type": t, "title": d.get("title")}
    if t == "memory_write":
        return {"type": t, "title": d.get("title"),
                "overwritten": bool(d.get("overwritten"))}
    if t == "memory_digest":
        return {"type": t, "n": len(d.get("cards") or []),
                "err": (d.get("err") or "")[:80]}
    if t == "bg_done":
        return {"type": t, "id": d.get("id"), "rc": d.get("rc"),
                "seconds": d.get("seconds"),
                "cmd_head": (d.get("cmd_head") or "")[:80]}
    if t == "turn_end":
        return {"type": t, "iters": d.get("iterations"),
                "usage": d.get("usage_total") or {}, "aborted": bool(d.get("aborted"))}
    if t == "auto_compact":
        return {"type": t}
    if t == "error":
        return {"type": t, "where": d.get("where"),
                "repr": (d.get("repr_") or "")[:200]}
    if t == "note_injected":
        return {"type": "note", "text": (d.get("text") or "")[:400]}
    return None


# ================================ 会话管理 ================================
class Sess:
    """一个 Web 会话 = 一个 AgentApp + 投影事件缓冲 + SSE 队列 + 挂起的问卡.
    E1: uid = 归属用户(超管/开放模式为 None), 会话列表按归属过滤."""

    def __init__(self, app, uid=None):
        self.app = app
        self.uid = uid
        self.sid = app.session.sid
        self.label = "%s/%s" % (app.provider.name, app.provider.model)
        self.lock = threading.Lock()
        self.buffer = []                  # [{seq, **投影}] 进程内全量(断线重放源)
        self.seq = 0
        self.queues = []                  # 各 SSE 客户端 queue.Queue
        self.pending = {}                 # ask_id -> {kind, ev, answer}
        self.busy = False
        self.last_touch = time.time()
        app.subscribe(self._sink)

    # ---- sink: 事件 → 投影 → 缓冲 + 广播(worker 线程调, 只做入队) ----
    def _sink(self, ev):
        try:
            p = project_web(ev)
            if p is None:
                return
            with self.lock:
                self.seq += 1
                p = dict(p, seq=self.seq)
                self.buffer.append(p)
                if len(self.buffer) > 4000:      # 内存护栏: 留尾
                    self.buffer = self.buffer[-2000:]
                qs = list(self.queues)
            for q in qs:
                try:
                    q.put_nowait(p)
                except queue.Full:
                    pass
        except Exception:
            pass                            # 红线1: sink 崩了不伤主流程

    def push_card(self, kind, data):
        """问卡(perm_ask/ask_user/ask_done)也走同一条 SSE 流."""
        self._sink({"type": "_card", "data": dict(data, kind=kind)})

    # ---- 问卡: worker 线程阻塞等人答, 随时可被中断解锁 ----
    def ask_perm(self, tool, args):
        if self.app.loop.abort.stop:
            return "deny"
        aid = "p%d" % int(time.time() * 1000 % 10 ** 9)
        rec = {"kind": "perm", "ev": threading.Event(), "answer": None}
        with self.lock:
            self.pending[aid] = rec
        self.push_card("perm_ask", {
            "id": aid, "name": tool.get("name"), "risk": tool.get("risk"),
            "args": json.dumps(args, ensure_ascii=False)[:600]})
        try:
            while not rec["ev"].wait(ASK_POLL):
                if self.app.loop.abort.stop:
                    return "deny"
            return rec["answer"] or "deny"
        finally:
            with self.lock:
                self.pending.pop(aid, None)

    def ask_user(self, question, header, options):
        if self.app.loop.abort.stop:
            return {"skipped": True}
        aid = "u%d" % int(time.time() * 1000 % 10 ** 9)
        rec = {"kind": "user", "ev": threading.Event(), "answer": None}
        with self.lock:
            self.pending[aid] = rec
        self.push_card("ask_user", {
            "id": aid, "question": str(question)[:1000],
            "header": str(header or "")[:120],
            "options": [str(o.get("label") if isinstance(o, dict) else o)[:60]
                        for o in (options or [])][:6]})   # 选项已由工具层清洗成 dict
        try:
            while not rec["ev"].wait(ASK_POLL):
                if self.app.loop.abort.stop:
                    return {"skipped": True}
            return rec["answer"] or {"skipped": True}
        finally:
            with self.lock:
                self.pending.pop(aid, None)

    def answer(self, aid, body):
        with self.lock:
            rec = self.pending.get(aid)
        if rec is None:
            return False
        if rec["kind"] == "perm":
            a = str(body.get("answer") or "deny")
            rec["answer"] = a if a in ("allow", "always", "deny") else "deny"
        else:
            rec["answer"] = {"label": body.get("label") or "",
                             "custom": body.get("custom") or "",
                             "skipped": bool(body.get("skipped"))}
        rec["ev"].set()
        self.push_card("ask_done", {"id": aid, "answer": rec["answer"]})
        return True

    def history(self):
        """重启前会话的兜底历史: canonical messages 平铺(user/assistant 有字才出)."""
        out = []
        for m in self.app.loop.messages:
            role, c = m.get("role"), m.get("content")
            if role in ("user", "assistant") and isinstance(c, str) and c.strip():
                out.append({"role": role, "content": c[:8000]})
        return out


class Hub:
    """全部 Web 会话的名册 + 逐出."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}

    def get(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        if s is not None:
            s.last_touch = time.time()
        return s

    def create(self, label, uid=None):
        prov = resolve_model(label)
        if prov is None:
            return None, "模型 %r 不可用 —— 去 /api/models 看清单或先加自增档" % label
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:      # 逐出最闲的
                old = min(self.sessions.values(), key=lambda s: s.last_touch)
                self.sessions.pop(old.sid, None)
                threading.Thread(target=old.app.shutdown, daemon=True).start()
        app = driver.AgentApp(prov, cwd=HERE, quiet=True, mode="web",
                              stream=True, auto_memory=True,
                              perms=perms_mod.Perms(ask_fn=None, auto="deny"))
        s = Sess(app, uid)
        app.perms.ask_fn = s.ask_perm     # perms 实例同一只, loop 侧即时生效
        app.ask_user = s.ask_user         # 双份挂(resume/clear 重建 loop 时带上)
        app.loop.ctx.ask_user = s.ask_user
        with self.lock:
            self.sessions[s.sid] = s
        return s, None

    def drop(self, sid):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s is not None:
            threading.Thread(target=s.app.shutdown, daemon=True).start()
        return s is not None

    def list(self, uid=None, admin=False):
        with self.lock:
            items = [{"sid": s.sid, "label": s.label, "busy": s.busy,
                      "owner": s.uid, "touch": int(s.last_touch)}
                     for s in self.sessions.values()
                     if admin or s.uid == uid]
        return sorted(items, key=lambda x: -x["touch"])


HUB = Hub()


# ================================ HTTP ================================
_EXT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-term-web/1.0"

    # ---- 小件 ----
    def log_message(self, fmt, *args):      # 安静: 只留错误
        if os.environ.get("AT_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")

    def _authed(self):
        """E1: 鉴权换成用户体系 —— 返回用户行(dict)或 None(拒).
        开放模式(没设 AT_WEB_TOKEN 且用户表空)谁都是超管(本机开发/自检);
        AT_WEB_TOKEN 本身 = 超管影子令牌(引导口)."""
        if getattr(self, "_user_cache", None) is not None:
            return self._user_cache
        h = self.headers.get("Authorization") or ""
        tok = h[7:].strip() if h.startswith("Bearer ") else ""
        if not tok:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tok = (q.get("token") or [""])[0]
        self._user_cache = webusers.auth(tok, TOKEN) or False
        return self._user_cache or None

    @staticmethod
    def _own(s, user):
        """会话归属: 本人或超管."""
        return user.get("role") == "admin" or s.uid == user.get("uid")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- 分发 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path.startswith("/api/"):
            user = self._authed()
            if user is None:
                return self._json(401, {"ok": False, "err": "令牌无效或未登录"})
            set_user(user.get("uid"))     # 本线程的 core 分域上下文
            return self._api_get(path, urllib.parse.parse_qs(u.query), user)
        return self._static(path)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self._body()
        if u.path == "/api/login":        # 登录不需要令牌
            ok, r = webusers.login(str(body.get("username") or ""),
                                   str(body.get("password") or ""))
            return self._json(200 if ok else 401, dict({"ok": ok},
                                                       **({"user": r} if ok else
                                                          {"err": r})))
        user = self._authed()
        if user is None:
            return self._json(401, {"ok": False, "err": "令牌无效或未登录"})
        set_user(user.get("uid"))
        m = re.match(r"^/api/sessions/([^/]+)/([^/]+)$", u.path)
        if m:
            s = HUB.get(m.group(1))
            if s is None:
                return self._json(404, {"ok": False, "err": "会话不存在(可能已重启)"})
            if not self._own(s, user):
                return self._json(403, {"ok": False, "err": "不是你的会话"})
            act = m.group(2)
            if act == "send":
                return self._send(s, body)
            if act == "interrupt":
                s.app.interrupt()
                return self._json(200, {"ok": True})
            if act == "memory":         # M2: 注入记忆卡进当前会话
                title = str(body.get("title") or "")
                content = memory_mod.read_card(title)
                if not content:
                    return self._json(404, {"ok": False, "err": "没有这张卡"})
                s.app.inject_memory(title, content)
                s.push_card("note", {"text": "· 已注入记忆卡: %s" % title})
                return self._json(200, {"ok": True})
            return self._json(404, {"ok": False, "err": "unknown action"})
        m = re.match(r"^/api/sessions/([^/]+)/ask/([^/]+)/answer$", u.path)
        if m:
            s = HUB.get(m.group(1))
            if s is None:
                return self._json(404, {"ok": False, "err": "会话不存在"})
            if not self._own(s, user):
                return self._json(403, {"ok": False, "err": "不是你的会话"})
            return self._json(200, {"ok": s.answer(m.group(2), body)})
        if u.path == "/api/sessions":
            s, err = HUB.create(str(body.get("model") or ""), user.get("uid"))
            if s is None:
                return self._json(400, {"ok": False, "err": err})
            return self._json(200, {"ok": True, "sid": s.sid, "label": s.label})
        if u.path == "/api/models":
            return self._post_model(body, user)
        if u.path == "/api/providers/key":     # E1.2: 家 key(超管=公共/用户=私有)
            name = str(body.get("name") or "").strip()
            key = str(body.get("key") or "").strip()
            try:
                provs = provider_mod.load_providers()
            except Exception:
                provs = {}
            if name not in provs:
                return self._json(404, {"ok": False, "err": "没有这家"})
            if user.get("role") == "admin":
                provider_mod.save_key(name, key)      # 整家单槽, 空=删
                src = "公共"
            else:
                _save_private_key(user.get("uid"), name, key)
                src = "私有"
            webusers.audit(user.get("username"), "provider_key",
                           "%s(%s)" % (name, src))
            return self._json(200, {"ok": True, "name": name,
                                    "key_mask": _mask(key), "key_src": src})
        if u.path == "/api/providers/fetch":   # E1.2: 拉这家真实可调清单
            name = str(body.get("name") or "").strip()
            try:
                provs = provider_mod.load_providers()
            except Exception:
                provs = {}
            p = provs.get(name)
            if p is None:
                return self._json(404, {"ok": False, "err": "没有这家"})
            pc = resolve_model(name) or copy.copy(p)
            try:
                models = provider_mod.fetch_models(pc, timeout=30)
            except Exception as ex:
                return self._json(200, {"ok": False,
                                        "err": "拉清单失败: %s" % ex})
            return self._json(200, {"ok": True,
                                    "models": [str(m)[:80] for m in models]})
        if u.path == "/api/admin/users":            # E1 超管: 开户
            if user.get("role") != "admin":
                return self._json(403, {"ok": False, "err": "仅超管"})
            ok, r = webusers.create_user(str(body.get("username") or ""),
                                         str(body.get("password") or ""),
                                         str(body.get("role") or "user"))
            return self._json(200 if ok else 400,
                              dict({"ok": ok}, **({"user": r} if ok else
                                                  {"err": r})))
        if u.path == "/api/admin/user":             # E1 超管: 停用/改密/重置令牌
            if user.get("role") != "admin":
                return self._json(403, {"ok": False, "err": "仅超管"})
            name = str(body.get("username") or "")
            op = str(body.get("op") or "")
            if op in ("disable", "enable"):
                ok, r = webusers.set_disabled(name, op == "disable")
            elif op == "password":
                ok, r = webusers.reset_password(name,
                                                str(body.get("password") or ""))
            elif op == "token":
                ok, r = webusers.reset_token(name)
            else:
                return self._json(400, {"ok": False, "err": "op 不认识"})
            return self._json(200 if ok else 400,
                              dict({"ok": ok}, **(r if ok else {"err": r})))
        return self._json(404, {"ok": False, "err": "unknown endpoint"})

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        user = self._authed()
        if user is None:
            return self._json(401, {"ok": False, "err": "令牌无效或未登录"})
        set_user(user.get("uid"))
        m = re.match(r"^/api/sessions/([^/]+)$", u.path)
        if m:
            s = HUB.get(m.group(1))
            if s is not None and not self._own(s, user):
                return self._json(403, {"ok": False, "err": "不是你的会话"})
            return self._json(200, {"ok": HUB.drop(m.group(1))})
        if u.path == "/api/models":       # E1.4: 删已加入的模型档
            body = self._body()
            label = str(body.get("label") or "").strip()
            fam = str(body.get("provider") or "").strip()
            model = str(body.get("model") or "").strip()
            if not ((fam and model) or label):
                return self._json(400, {"ok": False, "err": "给 label 或 家+型号"})
            ms = provider_mod.load_models()
            if fam and fam != "custom":   # 家内档按 (家, 型号)
                keep = [m for m in ms if not (m.get("provider") == fam
                                              and m.get("model") == model)]
            else:                         # 自增档按 label
                keep = [m for m in ms if not (m.get("provider") == "custom"
                                              and m.get("label") == label)]
            provider_mod.save_models(keep)     # 按身份路由公共/私有
            webusers.audit(user.get("username"), "model_delete",
                           label or "%s/%s" % (fam, model))
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False, "err": "unknown endpoint"})

    # ---- API 实现 ----
    def _api_get(self, path, q, user):
        if path == "/api/health":
            return self._json(200, {"ok": True, "demo": DEMO,
                                    "token_required": bool(TOKEN),
                                    "models": model_catalog()})
        if path == "/api/me":                 # E1: 我是谁(前端据此渲染超管面)
            return self._json(200, {"ok": True,
                                    "username": user.get("username"),
                                    "role": user.get("role")})
        if path == "/api/sessions":
            return self._json(200, {"ok": True, "sessions": HUB.list(
                user.get("uid"), user.get("role") == "admin")})
        if path == "/api/admin/users":        # E1 超管: 用户清单/审计
            if user.get("role") != "admin":
                return self._json(403, {"ok": False, "err": "仅超管"})
            return self._json(200, {"ok": True, "users": webusers.list_users()})
        if path == "/api/admin/audit":
            if user.get("role") != "admin":
                return self._json(403, {"ok": False, "err": "仅超管"})
            return self._json(200, {"ok": True,
                                    "audit": webusers.list_audit(
                                        int((q.get("limit") or ["100"])[0]))})
        m = re.match(r"^/api/sessions/([^/]+)/(messages|events)$", path)
        if m:
            s = HUB.get(m.group(1))
            if s is None:
                return self._json(404, {"ok": False, "err": "会话不存在(可能已重启)"})
            if not self._own(s, user):
                return self._json(403, {"ok": False, "err": "不是你的会话"})
            if m.group(2) == "messages":
                return self._json(200, {"ok": True, "messages": s.history()})
            return self._sse(s, q)
        if path == "/api/models":
            return self._json(200, {"ok": True, "models": model_catalog()})
        if path == "/api/providers":       # E1.2: 官方家清单(内置主流家)
            uid = user.get("uid")
            try:
                provs = provider_mod.load_providers()
            except Exception:
                provs = {}
            customs = {m.get("label") for m in provider_mod.load_models()
                       if m.get("provider") == "custom"}
            out = []
            for name, p in provs.items():
                if name in customs:
                    continue
                ok, src = _family_available(p, uid)
                out.append({"name": name, "family": p.family,
                            "base_url": p.base_url, "model": p.model,
                            "available": ok, "key_src": src,
                            "key_mask": _mask(
                                p.key if src == "公共"
                                else _private_key(uid, name))})
            return self._json(200, {"ok": True, "providers": out})
        # ---- M2 三只读页(忆库/技能/账本): core 现成模块的薄包装 ----
        if path == "/api/memory":
            import memory as mem_mod
            zone = (q.get("zone") or ["all"])[0]
            if zone not in ("archive", "forget"):
                zone = "all"
            return self._json(200, {"ok": True,
                                    "cards": mem_mod.list_cards(zone)})
        if path == "/api/memory/card":
            import memory as mem_mod
            title = (q.get("title") or [""])[0]
            content = mem_mod.read_card(title)
            if not content:
                return self._json(404, {"ok": False, "err": "没有这张卡"})
            return self._json(200, {"ok": True, "title": title,
                                    "content": content[:20000]})
        if path == "/api/skills":
            import skills as skills_mod
            return self._json(200, {"ok": True,
                                    "skills": skills_mod.list_skills()})
        if path == "/api/skill":
            import skills as skills_mod
            body, source = skills_mod.read_skill((q.get("name") or [""])[0])
            if not body:
                return self._json(404, {"ok": False, "err": "没有这份技能"})
            return self._json(200, {"ok": True, "body": body[:30000],
                                    "source": source})
        if path == "/api/ledger":
            import ledger as ledger_mod
            try:
                limit = int((q.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            return self._json(200, {"ok": True,
                                    "entries": ledger_mod.list_entries(limit),
                                    "aggregate": ledger_mod.aggregate()})
        return self._json(404, {"ok": False, "err": "unknown endpoint"})

    def _send(self, s, body):
        text = str(body.get("text") or "").strip()
        if not text:
            return self._json(400, {"ok": False, "err": "空消息"})
        with s.lock:
            if s.busy:
                return self._json(409, {"ok": False, "err": "本轮还在跑, 先等或中断"})
            s.busy = True

        def run():
            set_user(s.uid)             # E1: 回合线程的分域上下文(记忆/账本落本人)
            try:
                s.app.send(text)
            except Exception as ex:                    # 回合崩了也要解锁+告知
                s._sink({"type": "_card", "data": {"kind": "note", "text": (
                    "· 回合异常: %s: %s" % (type(ex).__name__, ex))[:300]}})
            finally:
                set_user(None)
                with s.lock:
                    s.busy = False
        threading.Thread(target=run, daemon=True).start()
        return self._json(200, {"ok": True})

    def _post_model(self, body, user):
        label = str(body.get("label") or "").strip()
        base = str(body.get("base_url") or "").strip().rstrip("/")
        key = str(body.get("key") or "").strip()
        model = str(body.get("model") or "").strip()
        fam = str(body.get("provider") or "").strip()
        if fam and fam != "custom":        # 家内档: {provider: 官方家, model}
            try:
                provs = provider_mod.load_providers()
            except Exception:
                provs = {}
            if fam not in provs:
                return self._json(400, {"ok": False, "err": "没有这家"})
            if not model:
                return self._json(400, {"ok": False, "err": "缺 model"})
            ms = [m for m in provider_mod.load_models()
                  if not (m.get("provider") == fam and m.get("model") == model)]
            ms.append({"provider": fam, "model": model})
            provider_mod.save_models(ms)
            return self._json(200, {"ok": True,
                                    "label": "%s/%s" % (fam, model),
                                    "scope": "公共档" if cur_uid() is None
                                    else "私有档"})
        if not (label and base and model):
            return self._json(400, {"ok": False,
                                    "err": "label/base_url/model 都要填"})
        if not re.match(r"^https?://", base):
            return self._json(400, {"ok": False, "err": "base_url 要 http(s):// 开头"})
        if body.get("delete"):
            ms = [m for m in provider_mod.load_models()
                  if not (m.get("provider") == "custom" and m.get("label") == label)]
            provider_mod.save_models(ms)
            return self._json(200, {"ok": True, "deleted": label})
        entry = {"provider": "custom", "label": label, "base_url": base,
                 "key": key, "model": model}
        for extra in ("vision", "imagegen", "family"):
            if body.get(extra):
                entry[extra] = body[extra]
        ms = [m for m in provider_mod.load_models()
              if not (m.get("provider") == "custom" and m.get("label") == label)]
        ms.append(entry)
        provider_mod.save_models(ms)                  # 按身份路由公共/私有档
        return self._json(200, {"ok": True, "label": label,
                                "key_mask": _mask(key),
                                "scope": "公共档" if cur_uid() is None
                                else "私有档"})

    # ---- SSE ----
    def _sse(self, s, q):
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        self.close_connection = True       # SSE 流结束即断, 不复用连接
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        out = queue.Queue(maxsize=1000)
        with s.lock:
            replay = [e for e in s.buffer if e["seq"] > since]
            s.queues.append(out)

        def send(obj):
            self.wfile.write(("data: %s\n\n" % json.dumps(
                obj, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()

        try:
            for e in replay:                          # 断线/晚到的补放
                send(e)
            if replay:
                self.wfile.write(b": replayed\n\n")
                self.wfile.flush()
            while True:
                try:
                    send(out.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # 心跳, 防中间层掐空闲
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with s.lock:
                if out in s.queues:
                    s.queues.remove(out)

    # ---- 静态前端(同源部署) ----
    def _static(self, path):
        if STATIC_DIR is None:
            return self._json(404, {"ok": False,
                                    "err": "静态目录不存在(只跑了 API 模式)"})
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        fp = os.path.normpath(os.path.join(STATIC_DIR, rel))
        inside = fp == STATIC_DIR or fp.startswith(STATIC_DIR + os.sep)
        if not inside or not os.path.isfile(fp):
            fp = os.path.join(STATIC_DIR, "index.html")   # SPA 式回退
            if not os.path.isfile(fp):
                return self._json(404, {"ok": False, "err": "not found"})
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _EXT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)


def build_server(port=0):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    global STATIC_DIR
    ap = argparse.ArgumentParser(description="agent-term Web 后端(纯标准库)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or 8787))
    ap.add_argument("--check", action="store_true", help="跑内置自检后退出")
    ap.add_argument("--static", default=None,
                    help="前端目录(默认 ../frontend, 存在才挂)")
    args = ap.parse_args(argv)
    if args.static:
        STATIC_DIR = os.path.normpath(args.static) \
            if os.path.isdir(args.static) else None
    if args.check:
        import selfcheck
        return selfcheck.main()
    if not TOKEN:
        print("[警告] 没设 AT_WEB_TOKEN —— 公网部署前务必设置, 否则任何人都"
              "能花你的 key.")
    httpd = build_server(args.port)
    print("agent-term web 后端已起: http://0.0.0.0:%d  (demo=%s, 前端=%s)"
          % (args.port, DEMO, STATIC_DIR or "未挂"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with HUB.lock:
            for s in list(HUB.sessions.values()):
                try:
                    s.app.shutdown()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

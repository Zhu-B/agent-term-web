# -*- coding: utf-8 -*-
"""instr.py — 事件总线与插桩落地(agent-term 的架构脊柱, M0 第一天就在).

agent loop 不直接 print: 每个关键点 emit 结构化事件, TUI 渲染 / JSONL 落盘 /
token 记账 / 将来的实验采集器都只是挂上来的 sink —— 这是对齐 Claude Code
"core 与 UI 解耦"的关键一步, 也是研究仪器定位的载体.

红线:
  1. sink 异常绝不向上抛 —— 插桩/渲染坏掉不能伤 agent 主流程(最多 stderr 提醒一句).
  2. 真实 API key 永不入事件流; 事件里只允许出现 env 名, 不允许出现值.
  3. trace 是 append-only JSONL, 每行独立完整 JSON 且即时 flush —— 断电后没有半行损坏.
  4. 图像本体永不入 trace/事件流(M15): 媒体落 media_dir(), 事件只记 sha1/路径/字节数.

事件 data 里的私有字段约定: 下划线开头的键(如 _raw/_content)只供消费型 sink 使用,
jsonl_sink 落盘前一律剥掉 —— 大对象走 raw_store_sink 单独落文件, 主 trace 不膨胀.
sink 挂载顺序必须保证消费型在前: [raw_store_sink, jsonl_sink, plain_sink].
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time

# ------ Paths ------
BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
TRACE_DIR = os.path.join(RUN_DIR, "trace")
RAW_DIR = os.path.join(TRACE_DIR, "raw")


def _cache_dir() -> str:
    """回放缓存目录. AT_CACHE_DIR 可重定向(_verify 自检用), 惰性取值."""
    return os.environ.get("AT_CACHE_DIR") or os.path.join(RUN_DIR, "cache")


def media_dir() -> str:
    """图像/媒体池塘(M15). AT_MEDIA_DIR 可重定向(自检/场景沙箱), 惰性取值."""
    return os.environ.get("AT_MEDIA_DIR") or os.path.join(RUN_DIR, "media")


_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
             "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp"}


def save_media(b64_data: str, mime: str = "image/png"):
    """base64 图像落媒体池(按内容 sha1 去重), 返回引用 ref={mime,sha1,bytes,path}.

    红线 4(M15): 本体只活在 media_dir(), trace/事件流/会话档只见引用。
    解码失败/空内容返回 None(调用方如实注记, 不炸)。"""
    try:
        raw = base64.b64decode(b64_data or "", validate=False)
    except Exception:
        return None
    if not raw:
        return None
    sha = hashlib.sha1(raw).hexdigest()
    ext = _MIME_EXT.get((mime or "").lower(), ".png")
    path = os.path.join(media_dir(), sha + ext)
    if not os.path.exists(path):
        try:
            os.makedirs(media_dir(), exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
        except OSError:
            return None
    return {"mime": mime or "image/png", "sha1": sha, "bytes": len(raw), "path": path}


def cache_get(sha: str):
    """AT_CACHE=1 时按请求 payload sha1 取缓存的历史响应, 没有/坏文件都返回 None."""
    path = os.path.join(_cache_dir(), sha + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_put(sha: str, raw) -> None:
    """历史响应按请求 sha1 落缓存(已存在不重写). 只在 AT_CACHE=1 时被调用."""
    d = _cache_dir()
    path = os.path.join(d, sha + ".json")
    if os.path.exists(path):
        return
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(raw, ensure_ascii=False, default=str))
    except Exception:
        pass  # 缓存写失败不伤主流程


def payload_sha1(payload) -> str:
    """同 payload 识别键(家法: sort_keys + ensure_ascii=False)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def sha1_text(text: str) -> str:
    """文本内容指纹(工具输出用, 与 payload_sha1 同法)."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


# ------ EventBus ------
class EventBus:
    """agent loop 每个关键点 emit 结构化事件, 逐 sink 分发.

    事件形: {"ts": float, "seq": int, "session": str, "type": str, "data": dict}
    事件类型: turn_start / llm_request / llm_response / tool_call / tool_result /
             perm_decision(M2) / turn_end / error
    """

    def __init__(self, session: str, sinks):
        self.session = session
        self.sinks = list(sinks)
        self._seq = 0
        self._lock = threading.Lock()

    def add_sink(self, sink) -> None:
        """运行中加 sink(M7: 前端/采集器 subscribe 用; 挂上即开始收后续事件)."""
        with self._lock:
            self.sinks.append(sink)

    def emit(self, type_: str, **data) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        ev = {"ts": time.time(), "seq": seq, "session": self.session,
              "type": type_, "data": data}
        for sink in self.sinks:
            try:
                sink(ev)
            except Exception as ex:  # 红线 1
                try:
                    sys.stderr.write("[instr] sink %s failed: %s\n" % (sink, ex))
                except Exception:
                    pass


# ------ Sinks ------
def raw_store_sink(dir_: str = RAW_DIR):
    """llm_response 的原始响应整体按 sha1 单独落文件, 主 trace 只记 raw_path.

    同 payload 重放/重试时内容相同即命中同一文件, 天然去重防膨胀.
    注意: 本 sink 会从事件里 pop 掉 _raw 并写入 raw_path —— 必须排在 jsonl_sink 之前.
    """
    os.makedirs(dir_, exist_ok=True)

    def sink(ev):
        if ev["type"] != "llm_response":
            return
        raw = ev["data"].pop("_raw", None)
        if raw is None:
            return
        path = os.path.join(dir_, "%s.json" % payload_sha1(raw))
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:  # 显式 utf-8, Windows 乱码第一坑
                f.write(json.dumps(raw, ensure_ascii=False, default=str))
        ev["data"]["raw_path"] = path

    return sink


def jsonl_sink(path: str):
    """append-only 事件轨迹(研究数据主载体). 每行 flush, 断电安全."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a", encoding="utf-8")

    def sink(ev):
        data = {k: v for k, v in ev["data"].items() if not k.startswith("_")}
        f.write(json.dumps({**ev, "data": data}, ensure_ascii=False, default=str) + "\n")
        f.flush()

    return sink


def plain_sink():
    """M0 的 UI: 事件流 → 人话终端输出. M1 的 tui.py 就是替换掉这个 sink.

    渲染形态(向 Claude Code 的 ●/⎿ 致敬):
      > 用户输入
        · provider/model 请求中…
        ● bash {"command": ...}
        ⎿ ok 0.4s · 132B
        ⎿ 输出前 8 行
    """
    st = {"streamed": False}      # M6: llm_chunk 逐字出过后, llm_response 不重打正文

    def sink(ev):
        t = ev["type"]
        d = ev["data"]
        if d.get("internal"):             # 内部机械调用(记忆整理等)不渲染
            return
        if d.get("sub"):
            return    # M14: 分身事件不进终端(结论在 spawn 的 tool_result 摘要里)
        if t == "turn_start":
            st["streamed"] = False
            print("\n> %s" % d.get("user_text", ""))
        elif t == "todo_update":
            print("[todo] " + " | ".join(
                "%s%s" % ({"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(
                    it.get("status"), "[?]"), it.get("content", ""))
                for it in d.get("items") or []))
        elif t == "llm_request":
            n = len((d.get("payload") or {}).get("messages") or [])
            print("  · %s/%s 请求中 (messages=%d)…" % (d.get("provider"), d.get("model"), n))
        elif t == "llm_chunk":
            txt = d.get("text") or ""
            if txt:
                sys.stdout.write(txt)      # 纯文本降级: 思考流也不上色
                sys.stdout.flush()
                st["streamed"] = True
        elif t == "llm_response":
            u = d.get("usage") or {}
            footer = "  ◷ %s %.1fs · in=%s out=%s%s" % (
                d.get("model"), d.get("latency_ms", 0) / 1000.0,
                u.get("prompt_tokens", "?"), u.get("completion_tokens", "?"),
                " (cached)" if d.get("cached") else "")
            if st["streamed"]:             # 正文已逐字出过: 收行+落款, 不重打
                st["streamed"] = False
                print(flush=True)
                print(footer)
                return
            print(footer)
            text = (d.get("message") or {}).get("content") or ""
            for line in text.splitlines():
                print("  | %s" % line)
        elif t == "tool_call":
            args = str(d.get("args_raw", "")).replace("\n", " ")
            print("  ● %s %s" % (d.get("name"), args[:120]))
        elif t == "tool_result":
            content = d.get("_content", "")
            flag = "ok" if d.get("ok") else "err"
            size = d.get("content_len", len(content))
            print("  ⎿ %s %.1fs · %dB%s" % (flag, d.get("duration_ms", 0) / 1000.0,
                                            size, " · 截断" if d.get("truncated") else ""))
            lines = content.splitlines() or ["(空)"]
            for line in lines[:8]:
                print("  ⎿ %s" % line[:100])
            if len(lines) > 8:
                print("  ⎿ …(还有 %d 行)" % (len(lines) - 8))
        elif t == "perm_decision":
            print("  ⚠ 权限: %s → %s" % (d.get("name"), d.get("decision")))
        elif t == "error":
            print("  ✗ %s: %s (attempt %s)" % (d.get("where"), d.get("repr_"),
                                                d.get("attempt")))

    return sink

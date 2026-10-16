# -*- coding: utf-8 -*-
"""session.py — 会话持久化: append-only JSONL transcript, 断电后每行独立完整.

行格式: {"ts": float, "kind": "message"|"meta"|"compact", "obj": dict}
  message — OpenAI chat 规范形消息(system/user/assistant/tool), resume 时按序回放
  meta    — provider/model/cwd 等环境描述(只记档, 不回放进上下文)
  compact — M5 /compact 的压缩标记(旧文件改名 .pre-compact 保留, 不改写)

红线:
  1. 只 append 不改写 —— 断电的半行本来就不是完整 JSON, 读回时跳过即可.
  2. 显式 encoding="utf-8", append 后立即 flush.
"""
from __future__ import annotations

import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE, "run", "sessions")


def _sid_new() -> str:
    return "s" + time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(2).hex()


class Session:
    def __init__(self, sid, path):
        self.sid = sid
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    # ------ Write ------
    def append(self, kind, obj):
        self._f.write(json.dumps({"ts": time.time(), "kind": kind, "obj": obj},
                                 ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self):
        """显式关句柄. M5 /compact 的 transcript 轮转需要先关再改名(Windows 上
        打开中的文件不能 rename)."""
        try:
            self._f.flush()
            self._f.close()
        except Exception:
            pass

    def meta(self, **kv):
        self.append("meta", kv)

    # ------ Read ------
    def messages(self):
        """按序回放 kind=message 的消息(坏行/半行跳过不崩 —— 断电自证)."""
        out = []
        for line in self._read_lines():
            if line.get("kind") == "message":
                out.append(line["obj"])
        return out

    def metas(self):
        return [line["obj"] for line in self._read_lines() if line.get("kind") == "meta"]

    def todos(self):
        """任务清单(M7): 取最后一条 kind=todo 行(全量写式, 后写覆盖前写)."""
        out = []
        for line in self._read_lines():
            if line.get("kind") == "todo":
                out = line["obj"]
        return out if isinstance(out, list) else []

    def _read_lines(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return []
        out = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue  # 半行/坏行: 断电现场, 跳过
        return out

    # ------ Factories ------
    @classmethod
    def new(cls, cwd="") -> "Session":
        sid = _sid_new()
        s = cls(sid, os.path.join(SESSIONS_DIR, sid + ".jsonl"))
        s.meta(event="session_start", cwd=cwd)
        return s

    @classmethod
    def resume(cls, path) -> "Session":
        """续既有会话: 同一文件继续 append, sid 从文件名取."""
        sid = os.path.splitext(os.path.basename(path))[0]
        s = cls(sid, path)
        s.meta(event="session_resume")
        return s

    @classmethod
    def latest(cls):
        """最近一个会话文件(按文件名排序=按 sid 时间), 没有则 None."""
        try:
            files = sorted(f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl"))
        except OSError:
            return None
        if not files:
            return None
        return os.path.join(SESSIONS_DIR, files[-1])

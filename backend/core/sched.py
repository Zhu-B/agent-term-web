# -*- coding: utf-8 -*-
"""sched.py — 定时任务(M27): 5 段 cron + 到点把 prompt 发进默认聊天窗.

口径:
- 表达式 = 标准 5 段 cron(分 时 日 月 周); 支持 *, 数字, a-b 区间, 逗号清单,
  */n 步进. 与标准 cron 的一处简化: 日 与 周 同时限定时按"且"匹配(标准是"或"),
  日常用法(每天X点/工作日X点/每月X号)无感.
- 周的编码: cron 惯例 0=周日; python weekday() 周一=0 —— 内部换算.
- next_fire 从给定时刻的下一分钟起逐分钟搜(上限 366 天), 找不到(如 2 月 30 日)
  返回 None. 逐分钟最坏 ~52 万次循环(~亚秒级), 典型当天内命中.
- due 判定: poll(start) 返回 (start, now] 之间到点的条目 —— 前端 30 秒 tick 一次,
  错过的整点也会补发一次(last_fire 防重发); recurring=false 的一次性任务发完即删.
- 存储 ~/.agent-term/schedule.json(AT_SCHED_FILE 测试缝); 前端开着才生效(REPL/
  批跑不跑表) —— 想要无人值守定时, 保持面板开着.

发出去的 prompt 走正常发送路: 那一轮的工具调用照常过权限门/钩子, 定时不绕门.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

SCHED_FILE = os.environ.get("AT_SCHED_FILE") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "schedule.json")

_LOCK = threading.Lock()
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))   # 分 时 日 月 周


def _parse_field(expr, lo, hi):
    """单段 cron 域 → 值集合. 坏表达式抛 ValueError."""
    values = set()
    for part in str(expr).strip().split(","):
        part = part.strip()
        if not part:
            raise ValueError("空段")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("步进必须>0")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(part)
            if "/" in expr and step > 1 and part != "*":
                b = hi                      # "n/step" 视为 n..hi/step(cron 惯例)
        if a < lo or b > hi or a > b:
            raise ValueError("越界 %r" % expr)
        values.update(range(a, b + 1, step))
    return values


def parse_cron(expr):
    """5 段 cron → 5 个 frozenset. 抛 ValueError(带人话)."""
    parts = str(expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError("要 5 段(分 时 日 月 周), 收到 %d 段: %r"
                         % (len(parts), expr))
    out = []
    for p, (lo, hi) in zip(parts, _RANGES):
        try:
            out.append(frozenset(_parse_field(p, lo, hi)))
        except ValueError as exc:
            raise ValueError("段 %r 不合法(%s)" % (p, exc)) from None
    return out


def _matches(fields, dt):
    minute, hour, dom, month, dow = fields
    return (dt.minute in minute and dt.hour in hour
            and dt.day in dom and dt.month in month
            and (dt.weekday() + 1) % 7 in dow)


def next_fire(expr, start=None):
    """start(默认 now) 之后下一个命中时刻; 366 天内无命中返回 None."""
    fields = parse_cron(expr)
    t = (start or datetime.now()) + timedelta(minutes=1)
    t = t.replace(second=0, microsecond=0)
    limit = t + timedelta(days=366)
    while t <= limit:
        if _matches(fields, t):
            return t
        t += timedelta(minutes=1)
    return None


# ---- 存储 ----

def _load(path=None):
    try:
        with open(path or SCHED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("tasks") if isinstance(data, dict) else None
        return [e for e in items if isinstance(e, dict)] if isinstance(items, list) else []
    except Exception:
        return []


def _save(entries, path=None):
    p = path or SCHED_FILE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"tasks": entries}, f, ensure_ascii=False, indent=2)


def list_entries(path=None):
    with _LOCK:
        return list(_load(path))


def create(expr, prompt, label="", recurring=True, path=None):
    """建条目. 返回 (entry, "") 或 (None, 人话错误)."""
    prompt = str(prompt or "").strip()
    if not prompt:
        return None, "prompt 不能为空(到点要发给 agent 的话)"
    try:
        parse_cron(expr)
    except ValueError as exc:
        return None, str(exc)
    with _LOCK:
        entries = _load(path)
        entry = {"id": "t%d" % (max([int(e["id"][1:]) for e in entries
                                     if str(e.get("id", "")[1:]).isdigit()] or [0]) + 1),
                 "cron": str(expr).strip(),
                 "prompt": prompt,
                 "label": str(label or "").strip() or prompt[:20],
                 "recurring": bool(recurring),
                 "created": time.time(),
                 "last_fire": None}
        entries.append(entry)
        _save(entries, path)
        return dict(entry), ""


def delete(tid, path=None):
    """删条目. 返回是否真的删了."""
    with _LOCK:
        entries = _load(path)
        left = [e for e in entries if e.get("id") != tid]
        if len(left) == len(entries):
            return False
        _save(left, path)
        return True


def poll(now=None, start=None, path=None):
    """返回 (start, now] 内到点的条目(带一次性删除与 last_fire 落盘).
    start 默认取条目上次轮询时刻(存在 last_poll 时由调用方传, 这里退化为 last_fire
    或 now-61s) —— 前端固定 30s tick 时传上一次 tick 的时刻即正确补发."""
    now = now or datetime.now()
    with _LOCK:
        entries = _load(path)
        fired, left = [], []
        for e in entries:
            ref = start
            if ref is None:
                lf = e.get("last_fire")
                ref = (datetime.fromisoformat(lf) if lf else None)
            hit = False
            if ref is None:
                # 从未发过: 只看"当前这一分钟是否命中"(不回放历史, 防新建即补发一堆)
                hit = _matches(parse_cron(e["cron"]), now) and now.second < 59
            else:
                nf = next_fire(e["cron"], ref)
                hit = nf is not None and nf <= now
            if hit:
                fired.append(dict(e))
                if e.get("recurring", True):
                    e["last_fire"] = now.isoformat(timespec="seconds")
                    left.append(e)
                # 一次性(recurring=false): 发完即删
            else:
                left.append(e)
        if fired:
            _save(left, path)
    return fired

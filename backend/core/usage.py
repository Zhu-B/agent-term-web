# -*- coding: utf-8 -*-
"""usage.py — token 记账(token_report.py 扫 JSONL 汇总的习惯, 内置化进平台).

数据源就是 instr 落的 trace: llm_response 事件里 provider/model/usage/cached 全有,
不用另立账本 —— trace 即账本(研究定位: 一份数据两用).

口径:
  cached 命中单列不并入真实用量(计划全局决策 3);
  price 估算读 providers.json 的 price_in/price_out, 单位按 元/百万 tokens 约定,
  没填(null)就不估, 绝不编数.
"""
from __future__ import annotations

import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(BASE, "run", "trace")


def report(trace_paths) -> dict:
    """聚合若干 trace 文件的用量. 返回 {"total": {...}, "by_model": {(provider,model): {...}}}"""
    by_model = {}
    for path in trace_paths:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") != "llm_response":
                continue
            d = ev.get("data") or {}
            key = (d.get("provider") or "?", d.get("model") or "?")
            u = d.get("usage") or {}
            row = by_model.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0,
                                            "cached_calls": 0})
            if d.get("cached"):
                row["cached_calls"] += 1
                continue  # 缓存命中不与真实用量混算
            row["calls"] += 1
            row["prompt"] += u.get("prompt_tokens") or 0
            row["completion"] += u.get("completion_tokens") or 0
    total = {"calls": 0, "prompt": 0, "completion": 0, "cached_calls": 0}
    for row in by_model.values():
        for k in total:
            total[k] += row.get(k, 0)
    return {"total": total, "by_model": by_model}


def fmt_report(rep, prices=None) -> str:
    """人话表格. prices: {(provider,model): (price_in, price_out)} 可选."""
    lines = []
    t = rep["total"]
    lines.append("用量: %d 次调用 · prompt %d / completion %d tokens%s"
                 % (t["calls"], t["prompt"], t["completion"],
                    (" · 另有缓存命中 %d 次(不计费)" % t["cached_calls"])
                    if t["cached_calls"] else ""))
    for (prov, model), row in sorted(rep["by_model"].items()):
        extra = ""
        if prices and (prov, model) in prices and prices[(prov, model)] != (None, None):
            pi, po = prices[(prov, model)]
            if pi is not None and po is not None:
                cost = row["prompt"] / 1e6 * pi + row["completion"] / 1e6 * po
                extra = " · 约 %.4f 元" % cost
        lines.append("  %-10s %-24s %d 次 · in %d / out %d%s"
                     % (prov, model, row["calls"], row["prompt"], row["completion"],
                        extra))
    return "\n".join(lines)


def context_level(trace_path):
    """当前上下文水位 = 最近一次请求的 prompt_tokens(None=没有请求).
    auto-compact 的水位线判断同源(不另立账)."""
    last = None
    try:
        with open(trace_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                if ev.get("type") == "llm_response":
                    d = ev.get("data") or {}
                    if d.get("sub"):
                        continue   # M14: 分身的水位不算父的水位(子是一次性小上下文)
                    pt = (d.get("usage") or {}).get("prompt_tokens")
                    if pt:
                        last = pt
    except OSError:
        return None
    return last


def report_for_session(trace_path) -> str:
    """REPL /usage 用: 单会话 trace → 人话表(带 providers.json 价格)."""
    rep = report([trace_path])
    prices = {}
    try:
        import provider as _p
        for name, p in _p.load_providers().items():
            prices[(name, p.model)] = (p.price_in, p.price_out)
    except Exception:
        pass
    return fmt_report(rep, prices)

# -*- coding: utf-8 -*-
"""ledger.py — 分包账本(M19): agent 每派一笔分身就记一条客观账.

定位(用户 10-14 拍板"持久分包账本档"): 跨会话可查的追加式 JSONL ——
{ts, sid(发起会话), parent(发起者模型), sub(分身标签), model(分身模型),
 task(任务头), ok, tokens, wall_s, result_head}. 只记客观项, 不做私有评分/
权重闭环(方案三全量版留作后续). 研究口径: 可回放、可导出(申博 B 候选
"注入仪器+账本"的实验仪器).

家法: 目录在 ~/.agent-term/ledger/(仓库外); AT_LEDGER_DIR 可重定向(测试缝);
追加式+即写即 flush(断电不丢半行, trace 同款红线); 模块级写锁(多聊天窗/分身
线程并发写); **账本写失败静默降级绝不伤 spawn 主流程**(sink 异常不上抛红线);
agent 的 read/write/bash 被存储守卫拦在 .agent-term 外 —— 只有本模块/ledger
工具/账本页能碰这个文件(与记忆卡同待遇).
"""
from __future__ import annotations

import json
import os
import threading
import time

LEDGER_DIR = os.environ.get("AT_LEDGER_DIR") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "ledger")

# 写锁: spawn 全走 worker 线程(单条也走) + 多聊天窗并发 —— 全进程串行化
_WRITE_LOCK = threading.RLock()

TASK_HEAD = 60          # 任务头截断(全文在 trace 里, 账本只留头)
RESULT_HEAD = 80        # 结论头截断


def _path() -> str:
    return os.path.join(LEDGER_DIR, "ledger.jsonl")


def append(entry: dict) -> bool:
    """追加一条账(自动带自增 id). 失败返回 False 不抛(挂账路径必须静默)."""
    try:
        os.makedirs(LEDGER_DIR, exist_ok=True)
        with _WRITE_LOCK:
            entry = dict(entry)
            entry["id"] = _next_id_locked()
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                f.flush()
        return True
    except OSError:
        return False


def _next_id_locked():
    """自增 id = 现有非 score 行数 + 1(锁内调用; 旧条目无 id 也数进去)."""
    try:
        with open(_path(), encoding="utf-8", errors="replace") as f:
            n = 0
            for ln in f.read().splitlines():
                if not ln.strip():
                    continue
                try:
                    if json.loads(ln).get("type") != "score":
                        n += 1
                except Exception:
                    continue
            return n + 1
    except Exception:
        return 1


def append_score(entry_id, score, reason) -> tuple:
    """追加一条私有评分(M21 环A): 独立行 {"type":"score","id":..,"score":..,
    "reason":..}, 不原地改条目(append-only 红线). 返回 (ok, err)."""
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        return False, "id 必须是数字(先 ledger list 看条目 id)"
    try:
        score = int(score)
    except (TypeError, ValueError):
        return False, "score 必须是 0-10 的整数"
    if not 0 <= score <= 10:
        return False, "score 只许 0-10"
    if not (reason or "").strip():
        return False, "reason 不能为空(一句理由)"
    ids = {r.get("id") for r in _read_lines() if r.get("type") != "score"}
    if entry_id not in ids:
        return False, "没有 id=%d 的分包账(先 ledger list 看条目)" % entry_id
    try:
        os.makedirs(LEDGER_DIR, exist_ok=True)
        with _WRITE_LOCK:
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "score", "id": entry_id,
                                    "score": score,
                                    "reason": reason.strip()},
                                   ensure_ascii=False) + "\n")
                f.flush()
        return True, ""
    except OSError as ex:
        return False, "写评分失败: %s" % ex


def _read_lines() -> list:
    """读全账(新→旧), 坏行跳过; score 行按 id 归并进条目的 _score 字段
    (最后一条评分生效, 私有: 只给工具链/聚合用, 页面不渲染)."""
    try:
        with open(_path(), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rows = []
    scores = {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("type") == "score":
            scores[r.get("id")] = r      # 文件序=旧→新, 后赋覆盖前值 = 最新评分生效
            continue
        rows.append(r)
    for r in rows:
        if r.get("id") in scores:
            r["_score"] = scores[r["id"]]
    rows.reverse()
    return rows


def list_entries(limit: int = 50, sid: str = "", model: str = "") -> list:
    """账目清单(新→旧). sid/model 非空 = 子串过滤(会话 id / 分身模型)."""
    rows = _read_lines()
    if sid:
        rows = [r for r in rows if sid in (r.get("sid") or "")]
    if model:
        rows = [r for r in rows if model in (r.get("model") or "")]
    return rows[:max(1, int(limit or 50))]


def aggregate() -> list:
    """按「发起者 → 分身模型」聚总: [{parent, model, calls, ok, tokens,
    scores_n, avg_score, last_score, last_reason}], 按笔数倒序.
    评分聚合 = 私有分域权重的可执行版(只进工具链, 页面不渲染)."""
    groups = {}
    order = []
    for r in _read_lines():
        key = (r.get("parent") or "?", r.get("model") or "?")
        if key not in groups:
            groups[key] = {"parent": key[0], "model": key[1], "calls": 0,
                           "ok": 0, "tokens": {"prompt_tokens": 0,
                                               "completion_tokens": 0,
                                               "total_tokens": 0},
                           "scores": []}
            order.append(key)
        g = groups[key]
        g["calls"] += 1
        if r.get("ok"):
            g["ok"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            g["tokens"][k] += (r.get("tokens") or {}).get(k, 0) or 0
        sc = r.get("_score")
        if sc is not None:
            g["scores"].append((sc.get("score"), sc.get("reason") or ""))
    out = []
    for k in order:
        g = groups[k]
        scs = g.pop("scores")
        g["scores_n"] = len(scs)
        g["avg_score"] = round(sum(s for s, _ in scs) / len(scs), 1) if scs else None
        g["last_score"] = scs[-1][0] if scs else None
        g["last_reason"] = scs[-1][1] if scs else ""
        out.append(g)
    out.sort(key=lambda g: g["calls"], reverse=True)
    return out


def fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"

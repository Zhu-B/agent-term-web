# -*- coding: utf-8 -*-
"""bgtask.py — 后台任务(M26): 长命令不堵 agent 轮, 跨轮取结果 + 完成通知.

口径(对齐 Claude Code 的 run_in_background/TaskOutput 最小内核):
- start(command) 立即返回 (id, 日志路径); 输出进 run/bg/<id>.log(追加),
  stdout/stderr 合流(2>&1) —— 不用 PIPE(写满缓冲区=死锁, MCP 同款教训),
  直接落文件天然免疫.
- daemon 看守线程 wait() 收工 → on_done(id, rc, seconds) 回调(事件总线发
  bg_done, 前端聊天流 dim 行通知, 模型下一轮用 bg 工具取输出).
- stop: terminate → 2s 不退 taskkill /T /F 树杀(Windows 防孤儿孙).
- 进程退出不追杀在跑任务(CC 同款: 后台任务独立于会话生死); 日志留存可回看.

测试缝: BG_DIR 模块属性(默认 run/bg), 测试指沙箱.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

BG_DIR = os.path.join("run", "bg")          # 相对 cwd; 测试改模块属性指沙箱

_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}                # id -> {cmd, proc, path, started, rc}
_SEQ = 0


def _next_id() -> str:
    global _SEQ
    with _LOCK:
        _SEQ += 1
        return "bg%d" % _SEQ


def start(command, cwd=None, on_done=None) -> tuple[str, str, dict]:
    """起一个后台任务. 返回 (id, 日志绝对路径, 任务元信息)."""
    os.makedirs(BG_DIR, exist_ok=True)
    tid = _next_id()
    path = os.path.abspath(os.path.join(BG_DIR, "%s.log" % tid))
    # Git Bash 选型同 tools._find_bash? 不 —— 这里与 bash 工具同一策略:
    # 交给 shell=True(Windows=cmd / POSIX=sh), 简单且与用户手敲一致.
    log = open(path, "ab")                  # 追加; 进程持有时 close 由看守做
    proc = subprocess.Popen(command, shell=True, cwd=cwd or None,
                            stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    log.close()                             # 父侧句柄即开即关, 子进程仍持有写端
    entry = {"cmd": command, "proc": proc, "path": path,
             "started": time.time(), "rc": None}
    with _LOCK:
        _TASKS[tid] = entry

    def _watch():
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        entry["rc"] = rc
        entry["seconds"] = round(time.time() - entry["started"], 2)
        if on_done is not None:
            try:
                on_done(tid, rc, entry["seconds"])
            except Exception:
                pass

    threading.Thread(target=_watch, daemon=True,
                     name="bgwatch-%s" % tid).start()
    return tid, path, info(tid)


def info(tid) -> dict:
    """单任务快照(不存在返回空 dict)."""
    with _LOCK:
        e = _TASKS.get(tid)
        if not e:
            return {}
    return {"id": tid, "cmd": e["cmd"], "log": e["path"],
            "running": e["rc"] is None, "rc": e["rc"],
            "started": round(e["started"], 0),
            "seconds": e.get("seconds")}


def list_tasks() -> list[dict]:
    with _LOCK:
        ids = sorted(_TASKS, key=lambda k: int(k[2:]) if k[2:].isdigit() else 0)
    return [info(i) for i in ids]


def output(tid, tail_bytes=20000) -> str:
    """读任务日志尾部(默认末 20KB). 尾读从半截行开始时砍掉第一行残行."""
    with _LOCK:
        e = _TASKS.get(tid)
    if not e:
        return ""
    try:
        size = os.path.getsize(e["path"])
        with open(e["path"], "rb") as f:
            if size > tail_bytes:
                f.seek(-tail_bytes, os.SEEK_END)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(data) < size and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def stop(tid) -> bool:
    """停任务(树杀). 返回是否真的动手了(任务不存在/已结束=False)."""
    with _LOCK:
        e = _TASKS.get(tid)
    if not e or e["rc"] is not None:
        return False
    proc = e["proc"]
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10,
                               stdin=subprocess.DEVNULL)
            else:
                proc.kill()
    except Exception:
        pass
    return True

# -*- coding: utf-8 -*-
"""hooks.py — 工具前后可编程钩子(M25, 对齐 Claude Code hooks 的最小内核).

settings.json 的 "hooks" 节:
  {"hooks": {
      "pre_tool":  [{"match": "bash", "command": "python guard.py"}],
      "post_tool": [{"match": "write", "command": "python log.py"}]}}
  match = 工具名 glob(fnmatch); command 走 shell(用户自己的脚本).

执行口径:
- 钩子子进程从 stdin 收 JSON: pre = {tool, args} / post = {tool, args, ok,
  content_head(前 400 字)}; stdout 可回一行 JSON {"decision":"deny",
  "reason":"..."} —— 只有 pre 的 deny 生效, 否决消息作为 error ToolRet 回喂
  模型(与权限拒绝同一条自纠通道, 不抛异常).
- 失败放行(fail-open): 崩溃/超时(默认 10s)/输出不合法 = 不拦, 事件记 error ——
  钩子是旁路治理, 不能把 agent 卡死; 要硬拦走权限规则(M23 deny).
- 事件 hook_run{phase, match, rc, seconds, decision, error} 进 trace(研究口);
  前端只对 deny/error 渲染提示行, 放行的钩子零噪音.

挂载口径: 前端聊天窗(open_chat)与 REPL(one-shot 同)构造时注入 load_hooks()
快照(改 hooks 新开窗生效); runlab(实验自变量)/客制化(三层防线不被旁路)/
runflow(每跑独立勾选)不挂. 测试进程把 HOOKS_PATH 指到沙箱灭用户环境方差
(golden 确定性, 与 perms.RULES_PATH 同款口径).
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time

HOOK_TIMEOUT = 10.0
PHASES = ("pre_tool", "post_tool")

# 与 perms.RULES_PATH 同一处 settings.json; 测试改模块属性指沙箱
HOOKS_PATH = os.environ.get("AT_SETTINGS") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "settings.json")


def load_hooks(path=None):
    """读 settings.json 的 hooks 节 → {phase: [{"match","command"}, ...]}.
    坏文件/坏条目=该条丢弃, 不抛(钩子不能因为配置坏就把 agent 弄挂)."""
    try:
        with open(path or HOOKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        node = data.get("hooks") if isinstance(data, dict) else None
    except Exception:
        return {p: [] for p in PHASES}
    out = {p: [] for p in PHASES}
    if isinstance(node, dict):
        for phase in PHASES:
            items = node.get(phase)
            if not isinstance(items, list):
                continue
            for it in items:
                if (isinstance(it, dict)
                        and str(it.get("match") or "").strip()
                        and str(it.get("command") or "").strip()):
                    out[phase].append({"match": str(it["match"]).strip(),
                                       "command": str(it["command"]).strip()})
    return out


def entries_for(hooks, phase, name):
    """该阶段命中该工具名的钩子(match 是工具名 glob)."""
    if not hooks:
        return []
    return [e for e in (hooks.get(phase) or [])
            if fnmatch.fnmatchcase(name, e["match"])]


def run_hook(entry, payload, timeout=HOOK_TIMEOUT):
    """跑一个钩子: 子进程收 JSON 于 stdin; 返回 meta dict(给事件用).
    decision 只认 stdout 里合法 JSON 的 {"decision":"deny","reason":...}."""
    meta = {"match": entry["match"], "rc": None, "seconds": 0.0,
            "decision": None, "error": ""}
    t0 = time.time()
    try:
        proc = subprocess.run(
            entry["command"], shell=True,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=timeout)
        meta["rc"] = proc.returncode
        meta["seconds"] = round(time.time() - t0, 2)
        if proc.stdout:
            try:
                data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
            except ValueError:
                data = None
            if isinstance(data, dict) and str(data.get("decision") or "") == "deny":
                meta["decision"] = "deny"
                meta["reason"] = str(data.get("reason") or "钩子否决(未给理由)")[:400]
    except subprocess.TimeoutExpired:
        meta["seconds"] = round(time.time() - t0, 2)
        meta["error"] = "超时(%.0fs)" % timeout
    except Exception as exc:
        meta["seconds"] = round(time.time() - t0, 2)
        meta["error"] = "%s: %s" % (type(exc).__name__, exc)
    return meta


def run_pre(hooks, name, args, timeout=HOOK_TIMEOUT):
    """跑全部命中的 pre 钩子(串行, 先到先得). 返回 (allowed, deny_reason, metas).
    失败放行: 崩/超时/不合法输出都不拦; 第一个 deny 生效."""
    metas, deny = [], None
    for entry in entries_for(hooks, "pre_tool", name):
        meta = run_hook(entry, {"tool": name, "args": args}, timeout=timeout)
        metas.append(meta)
        if meta["decision"] == "deny" and deny is None:
            deny = meta.get("reason", "钩子否决")
    return (deny is None), (deny or ""), metas


def run_post(hooks, name, args, ok, content_head, timeout=HOOK_TIMEOUT):
    """跑全部命中的 post 钩子(结果摘要喂它们); 只记事件, 无否决语义."""
    metas = []
    for entry in entries_for(hooks, "post_tool", name):
        payload = {"tool": name, "args": args, "ok": bool(ok),
                   "content_head": str(content_head or "")[:400]}
        metas.append(run_hook(entry, payload, timeout=timeout))
    return metas

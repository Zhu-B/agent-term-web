# -*- coding: utf-8 -*-
"""perms.py — 权限门(对应 Claude Code 的权限子系统, M2 版; M23 扩持久规则+只读模式).

风险分级(工具 ToolDef 的 "risk" 字段):
  read  — 只读工具(read/grep/glob) 恒放行
  write — 改文件(write/edit) 默认问人
  exec  — 执行命令(bash) 默认问人

决策序(M23):
  deny 持久规则 → allow 持久规则 → 只读模式拒写/执行 → risk:read 放行 →
  本会话 always 放行表 → ask_fn 问人 → 默认拒绝.

持久规则(settings.json 的 "perm" 节, 跨会话; 对齐 CC 的 allow/deny 清单):
  {"perm": {"allow": ["bash:git status*", "web_fetch"], "deny": ["bash:rm *"]}}
  规则形态 "工具名" 或 "工具名:参数glob"(fnmatch); 工具名本身也吃通配(*).
  参数串口径: bash 用 command 本体; 其它工具 "k=v" 空格连排(键序, 截 200 字).
  适用口径: REPL/one-shot/前端聊天窗 自动生效; 客制化(M12b 三层防线, allow 规则
  不得绕开受保护硬拒)/runflow(每跑独立勾选)/runlab(实验自变量) 构造时传 rules={}
  显式关闭. 测试进程把 RULES_PATH 指到沙箱, 灭用户环境方差(golden 确定性).

只读模式(mode="readonly"): write/exec 一律拒(人话理由回喂模型), read 不受影响;
聊天窗状态区「只读」胶囊 live 切换 —— 对齐 CC 权限模式(只读/计划态)的最小内核.

ask_fn(tool, args) -> "allow" | "always" | "deny" | None(None 当 deny):
  交互 REPL = tui.confirm 弹 [y]允许 [a]本会话总允许 [n]拒绝;
  one-shot 无 stdin 可问 → auto 策略: 默认全拒(--yes 才放行).
拒绝不是异常: 拒绝消息作为 error ToolRet 回喂模型, 它会换只读途径或问用户 —— 这和
工具报错自纠是同一条通道.

红线: 权限判定永远在本进程内存里完成, 不联网; 持久规则只存 settings.json(仓库外),
always 表只活在当前会话.
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading

# 默认与 provider.SETTINGS_STORE 同一处; 测试改模块属性指沙箱(勿改 provider 的).
RULES_PATH = os.environ.get("AT_SETTINGS") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "settings.json")


def load_rules(path=None):
    """读 settings.json 的 perm 节 → {"allow": [...], "deny": [...]}. 坏文件=无规则."""
    try:
        with open(path or RULES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        node = data.get("perm") if isinstance(data, dict) else None
    except Exception:
        return {"allow": [], "deny": []}
    out = {"allow": [], "deny": []}
    if isinstance(node, dict):
        for side in ("allow", "deny"):
            items = node.get(side)
            if isinstance(items, list):
                out[side] = [x.strip() for x in items
                             if isinstance(x, str) and x.strip()]
    return out


def render_args(tool, args):
    """规则匹配用的参数串: bash=命令本体; 其它工具 k=v 空格连排(键序, 截 200)."""
    if tool.get("name") == "bash":
        return str((args or {}).get("command") or "")
    parts = ["%s=%s" % (k, args[k]) for k in sorted(args or {})]
    return " ".join(parts)[:200]


def rule_matches(rule, name, argstr):
    """单条规则匹配: "工具名" 或 "工具名:参数glob"; 两段都吃 fnmatch 通配."""
    rule = (rule or "").strip()
    if not rule:
        return False
    if ":" in rule:
        rtool, pattern = rule.split(":", 1)
    else:
        rtool, pattern = rule, None
    if not fnmatch.fnmatchcase(name, rtool.strip()):
        return False
    if pattern is None or pattern.strip() == "*":
        return True
    return fnmatch.fnmatchcase(argstr, pattern.strip())


class Perms:
    def __init__(self, ask_fn=None, auto="deny", rules=None, mode="normal"):
        self.ask_fn = ask_fn   # (tool: dict, args: dict) -> "allow"|"always"|"deny"|None
        self.auto = auto       # ask_fn 为空时的自动策略: "deny"(默认, 安全) | "allow"(--yes)
        self.always = set()    # 本会话内用户选过"总允许"的工具名
        # M23: rules=None → 自动读 settings.json(跨会话); {} → 显式关闭(客制化/
        # runflow/runlab/测试沙箱); 传 {"allow":[..],"deny":[..]} → 注入(测试).
        self._rules_injected = rules
        if rules is None:
            self.rules = load_rules()
        else:
            self.rules = {"allow": list(rules.get("allow") or []),
                          "deny": list(rules.get("deny") or [])}
        self._rules_mtime = self._stat_rules()
        self.mode = mode       # "normal" | "readonly"(聊天窗「只读」胶囊 live 切)
        # M14: 分身并行时多线程问人必须串行 —— REPL 的 input() 并发会交错提示行
        # 并串读 stdin 把答案配错(前端弹框本来天然串行, 加锁无副作用).
        self._ask_lock = threading.Lock()

    def _stat_rules(self):
        """settings.json 的 mtime(文件不存在=None). 热刷新的比较基准."""
        try:
            return os.path.getmtime(RULES_PATH)
        except OSError:
            return None

    def _maybe_reload(self):
        """设置页改了规则 → 已开窗即时生效: decide 前 stat 一次, mtime 变了重读.
        stat 微秒级; 注入/关闭口径不刷. 注: 同秒双写 mtime 可能并列(秒粒度文件系统)
        —— 设置编辑是人手节奏, 实际撞不上, 撞上下一次 decide 也会补上."""
        if self._rules_injected is not None:
            return
        mtime = self._stat_rules()
        if mtime != self._rules_mtime:
            self._rules_mtime = mtime
            self.rules = load_rules()

    def decide(self, tool, args):
        """返回 (verdict, allowed_by): verdict ∈ allow/deny,
        allowed_by ∈ rule:deny:<规则> / rule:allow:<规则> / readonly-mode /
        risk:read / session-always / user-allow / user-always / auto:<策略>."""
        self._maybe_reload()
        risk = tool.get("risk", "write")   # 未知工具按最高风险处理
        name = tool.get("name", "?")
        argstr = render_args(tool, args or {})
        # 持久规则最先: deny 压一切(含只读工具, 如 deny "web_fetch" 或 deny "read:*密*");
        # allow 紧随 —— 对 write/exec 意味着免问放行, 所以只该放用户自己写的规则.
        for rule in self.rules.get("deny", []):
            if rule_matches(rule, name, argstr):
                return "deny", "rule:deny:%s" % rule
        for rule in self.rules.get("allow", []):
            if rule_matches(rule, name, argstr):
                return "allow", "rule:allow:%s" % rule
        if self.mode in ("readonly", "plan") and risk in ("write", "exec"):
            # plan=计划模式(M24): 写/执行同样拒, 但由 plan_submit 审批通过后翻回 normal
            return "deny", "%s-mode" % self.mode
        if risk == "read":
            return "allow", "risk:read"
        if name in self.always:
            return "allow", "session-always"
        if self.ask_fn is None:
            return ("allow" if self.auto == "allow" else "deny"), "auto:%s" % self.auto
        with self._ask_lock:               # 快路径(上面几条)不进锁; 只有真问人才串行
            if name in self.always:        # 等锁期间别的子已选了"总允许": 不再重复问
                return "allow", "session-always"
            ans = self.ask_fn(tool, args or {})
        if ans == "always":
            self.always.add(name)
            return "allow", "user-always"
        if ans == "allow":
            return "allow", "user-allow"
        return "deny", "user-deny"

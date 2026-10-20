# -*- coding: utf-8 -*-
"""tools.py — 工具注册表 + 内置工具(M0 范围: bash / read; write/edit/grep/glob/todo
是 M2/M5 的活, 挂进 BUILTIN 列表即可).

铁律(从 wechat-eye/server.py 原样搬, 血泪教训):
  bash 的子进程必须 stdin=DEVNULL —— 子进程若继承我们的 stdin, 会吃掉待读的通道输入.
  (M4 的 MCP client 用 stdin=PIPE 与此不矛盾: 那是我们主动喂的协议通道, 这里防的是意外继承.)

工具错误处理约定:
  一律返回 error ToolRet(ok=False, content=错误说明) 回喂模型 —— 模型看到错误后通常
  自己换姿势重试, 这是 agent 自纠能力的来源; 绝不向上抛异常炸掉整轮.
"""
from __future__ import annotations

import fnmatch
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import time


# ------ Types ------
class ToolRet:
    def __init__(self, ok, content, truncated=False, images=None):
        self.ok = ok
        self.content = content        # 恒为字符串(全仓按 str 消费: len/sha1/splitlines)
        self.truncated = truncated
        self.images = images          # M15: [ref{mime,sha1,bytes,path}] 图像引用(本体在
                                      # run/media); 附不附图由模型能力在回填缝决定


class ToolCtx:
    """工具执行的上下文. session/perms 等字段后续里程碑陆续启用, M0 只用 cwd."""

    def __init__(self, cwd, bus=None, abort=None, session=None, perms=None,
                 ask_user=None, platform=None, memory_overwrite=None,
                 spawn=None, sub=False, sub_provider_fn=None, sub_models=None,
                 sub_allow_other=None, image_gen=None, hooks=None,
                 sub_vision_models=None, look_image=None):
        self.cwd = cwd
        self.bus = bus
        self.abort = abort
        self.session = session
        self.perms = perms
        self.ask_user = ask_user   # (question, header, options)->选择; None=无选择界面
        self.platform = platform   # (action, **params)->{"ok","result"/"err"}; None=无界面
        self.memory_overwrite = memory_overwrite   # "ask"/"keep"/"auto" 或 callable; 设置页
        self.spawn = spawn   # M14: loop 注入的绑方法 (prompt,label,model)->ToolRet; None=未启用分身
        self.sub = sub   # M14: 本 ctx 属于分身子代理(深度守卫: 子不能再派)
        self.sub_provider_fn = sub_provider_fn   # M14: label("家/型号")->Provider; None=无解析器
        self.sub_models = sub_models   # M14: 可调用清单(list 或 callable)——给 spawn 报错时
                                       # 回喂候选名, 模型看清单自纠(写错名不用猜)
        self.sub_allow_other = sub_allow_other   # M14: 设置页「分身是否允许用非当前对话模型」
                                       # (bool 或 callable, 实时) —— 关时 spawn 点名 model 直接
                                       # 拒(回喂原因); 开时由模型自己按任务挑
        self.image_gen = image_gen   # M15: loop 注入绑方法 (prompt,image,model)->ToolRet;
                                     # None=本环境没配画图档(工具层报错指路)
        self.sub_vision_models = sub_vision_models   # M32: 视觉档候选清单(list 或
                                       # callable, 前端接线; look_image 报错回喂用, 没接
                                       # 退回 sub_models 全清单)
        self.look_image = look_image   # M32: loop 注入绑方法 (path,question,model)->ToolRet;
                                       # None=未启用识图(工具层报错指路)
        self.hooks = hooks   # M26: {pre_tool,post_tool} 快照(bg 工具的 run 门复用)
        self.todos = []   # M5: todo 工具的会话内任务清单状态


class ToolRegistry:
    """ToolDef = dict(name/description/input_schema/risk/run) —— 对齐 wechat-eye
    的 TOOLS 家法, MCP 工具(M4)转成同构 dict 挂进来即可一视同仁."""

    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool["name"]] = tool

    def get(self, name):
        return self.tools.get(name)

    def view_of(self, exclude=()):
        """M14: 共享 tool dict 的浅视图(注册表本身无状态) —— 子代理视图排掉 spawn,
        否则 schemas()/工具清单会把 spawn 发给子模型(深度守卫只在 dispatch 层, 白烧一轮)."""
        v = ToolRegistry()
        v.tools = {k: t for k, t in self.tools.items() if k not in exclude}
        return v

    def schemas(self):
        """转 OpenAI tools 参数. 调用方注意: 返回空列表时整键不发(见 provider.build_payload)."""
        return [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in self.tools.values()]

    def dispatch(self, name, args_raw, ctx):
        """按名分发. arguments 宽容解析: 坏 JSON → error ToolRet 回喂自纠, 绝不崩."""
        tool = self.tools.get(name)
        if tool is None:
            return ToolRet(False, "unknown tool: %s (可用工具: %s)"
                           % (name, ", ".join(sorted(self.tools))))
        ok, args = parse_args(args_raw)
        if not ok:
            return ToolRet(False, "arguments 不是合法 JSON 对象: %r" % args_raw[:200])
        try:
            return tool["run"](args, ctx)
        except Exception as ex:
            return ToolRet(False, "tool crashed: %s: %s" % (type(ex).__name__, ex))


def parse_args(args_raw):
    """宽容解析工具 arguments → (ok, dict). agent 层问权限也用它拿 args 做展示."""
    if isinstance(args_raw, str):
        s = args_raw.strip()
        try:
            args = json.loads(s) if s else {}
        except Exception:
            return False, {}
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if isinstance(args, dict):
        return True, args
    return False, {}


# ------ Helpers ------
def _clip(text, head=30, tail=10, max_chars=16000):
    """超长输出截断: head N 行 + …(还有 X 行) + tail M 行, 再按字符数封顶."""
    lines = text.splitlines()
    out, truncated = text, False
    if len(lines) > head + tail + 2:
        out = "\n".join(lines[:head] + ["…(还有 %d 行)" % (len(lines) - head - tail)]
                        + lines[-tail:])
        truncated = True
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…(超出 %d 字符截断)" % max_chars
        truncated = True
    return out, truncated


def _resolve(ctx, path):
    return os.path.abspath(os.path.join(ctx.cwd, path)) if path else ""


# ------ Tool: bash ------
def _decode_console(b: bytes) -> str:
    """子进程输出解码: utf-8 严格优先(python/现代工具), 失败回退 gb18030(cmd.exe 的
    dir 等在中文 Windows 上输出 GBK, 按 utf-8 解会乱码 —— E2E 实测的坑)."""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(os.environ.get("AT_FALLBACK_ENCODING", "gb18030"), "replace")


# WSL 启动器所在的目录名: 那个 bash.exe 是 WSL 的入口, 活在另一个文件系统世界,
# 且启动时往 stderr 吐 UTF-16 的 WSL 警告(如 localhost 代理提示), 会污染每次工具
# 输出(同事机器实测: echo 9B 变 145B, 回归网 DIFF 抓获) —— 选 bash 时跳过
WSL_LAUNCHER_DIRS = {"windowsapps", "system32", "syswow64", "wsl"}


def _find_bash():
    """PATH 顺序找真 bash(优先 Git Bash), 跳过 WSL 启动器; 找不到返回 None.

    WSL 启动器两条判据: ①目录名在名单里(WindowsApps/System32/SysWOW64/WSL);
    ②同目录躺着 wsl.exe(Store 版 WSL 装在 Program Files\\WSL, 目录名不一定在
    名单里). 同事机实测漏网后果: bash 工具整个跑进 /mnt 世界, 与 Windows 侧的
    cwd/路径互相不可达, powershell 也不在 WSL PATH —— 宁可退 cmd.exe 也不用它."""
    if os.name != "nt":     # macOS/Linux: PATH 里的 bash 就是真 bash(无 .exe 后缀)
        return shutil.which("bash") or shutil.which("sh")
    for dir_ in os.environ.get("PATH", "").split(os.pathsep):
        if not dir_:
            continue
        cand = os.path.join(dir_, "bash.exe")
        if not os.path.isfile(cand):
            continue
        if os.path.basename(os.path.normpath(dir_)).lower() in WSL_LAUNCHER_DIRS:
            continue
        if os.path.isfile(os.path.join(dir_, "wsl.exe")):
            continue  # bash 与 wsl 同目录 = WSL 入口, 不是真 bash
        return cand
    return None


def _bash_argv(cmd):
    """有 Git Bash 就走它(POSIX 语法+通配符展开, Claude Code 在 Windows 同款做法);
    argv 直传不经 shell, 免 cmd.exe 的引号/通配符两层坑. 只有 WSL 启动器或啥都
    没有时才退回 cmd.exe."""
    bash = _find_bash()
    if bash:
        return [bash, "-c", cmd], False
    return cmd, True  # shell=True → cmd.exe


# C:\ 式 Windows 路径 → bash 模式下要给模型加提示(E2E 实测坑: 反斜杠被当转义符吃掉)
_WIN_BACKSLASH_PATH = re.compile(r"[A-Za-z]:\\")


def run_bash(args, ctx):
    cmd = args.get("command") or args.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return ToolRet(False, "missing 'command' (string)")
    try:
        timeout = int(args.get("timeout", 120))
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(5, min(timeout, 600))
    if _cites_secret_store(cmd):
        return ToolRet(False, "命令里提到本地 key/配置存储(.agent-term / keys.json / "
                              "providers.json / models.json) —— 硬拒: key 绝不进对话"
                              "(防随请求外泄给中转通道); 要看配置请在界面里操作.")
    argv, use_shell = _bash_argv(cmd)
    try:
        # stdin=DEVNULL 是铁律: 见模块 docstring / wechat-eye 的血泪注释
        r = subprocess.run(argv, shell=use_shell, capture_output=True, timeout=timeout,
                           cwd=ctx.cwd, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ToolRet(False, "timeout after %ds: %s" % (timeout, cmd[:200]))
    out = _decode_console(r.stdout or b"")
    err = _decode_console(r.stderr or b"")
    # 行尾统一: cmd.exe 输出 CRLF, bash 是 LF —— 归一成 LF, 跨机器输出才一致
    out = out.replace("\r\n", "\n")
    err = err.replace("\r\n", "\n")
    if not use_shell and _WIN_BACKSLASH_PATH.search(cmd):
        # 给模型的纠错信号(E2E 实测: glm 拿 C:\Users 喂 bash -c, 反斜杠被当转义符吃掉)
        out += "\n(hint: 这台机器的 bash 会把反斜杠当转义符吃掉, Windows 路径请用" \
               "正斜杠 C:/Users/... 或 /c/Users/...; dir /s /b 等 cmd 语法也不可用, 用 ls/find)"
    text = out.rstrip("\n")
    if err.strip():
        text += ("\n[stderr]\n" if text else "") + err.rstrip("\n")
    if not text:
        text = "(no output, exit code %d)" % r.returncode
    elif r.returncode != 0:
        text += "\n(exit code %d)" % r.returncode
    clipped, truncated = _clip(text)
    return ToolRet(True, clipped, truncated)


BASH = {
    "name": "bash",
    "description": ("执行一条 shell 命令并返回 stdout+stderr. 走 bash(Git Bash): 用 POSIX 语法 "
                    "(ls/wc/grep/find/cat, 通配符正常展开, dir/type 等 cmd 语法不可用). "
                    "Windows 路径必须用正斜杠: C:/Users/... 或 /c/Users/... (反斜杠会被 bash 转义吃掉). "
                    "用于列目录、跑脚本、查进程、git 等一切命令行活. "
                    "输出超长自动截断(头30行+尾10行), 默认 120 秒超时. "
                    "命令收不到交互输入, 不要发会等待输入的命令."),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令, POSIX/bash 语法"},
            "timeout": {"type": "integer", "description": "超时秒数, 5-600, 默认 120"},
        },
        "required": ["command"],
    },
    "risk": "exec",
    "run": run_bash,
}


# ------ Tool: read ------
def run_read(args, ctx):
    path = args.get("path") or args.get("file_path") or ""
    full = _resolve(ctx, path)
    if not full:
        return ToolRet(False, "missing 'path'")
    if _is_secret_store(full):
        return ToolRet(False, "不许读本地存储(keys/模型库/配置) —— 防 key 经对话"
                              "外泄给中转通道; 要看配置请在界面里操作.")
    if not os.path.exists(full):
        return ToolRet(False, "no such file: %s (cwd=%s)" % (full, ctx.cwd))
    if os.path.isdir(full):
        names = sorted(os.listdir(full))
        clipped, truncated = _clip("\n".join(names) or "(空目录)")
        return ToolRet(True, clipped, truncated)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as ex:
        return ToolRet(False, "read failed: %s" % ex)
    try:
        offset = max(1, int(args.get("offset", 1)))
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(args.get("limit", 2000))
    except (TypeError, ValueError):
        limit = 2000
    limit = max(1, min(limit, 2000))
    sel = lines[offset - 1:offset - 1 + limit]
    body = "\n".join("%6d\t%s" % (offset + i, ln) for i, ln in enumerate(sel))
    if not body:
        body = "(空文件或 offset 超出范围, 共 %d 行)" % len(lines)
    truncated = offset > 1 or offset - 1 + limit < len(lines)
    clipped, c2 = _clip(body, head=2000, tail=0, max_chars=64000)
    return ToolRet(True, clipped, truncated or c2)


READ = {
    "name": "read",
    "description": ("读一个文本文件, 返回带行号的内容(cat -n 风格, tab 分隔). 默认从头读 2000 行, "
                    "大文件用 offset(1 起始行号)/limit 控制分段读. 相对路径相对当前工作目录. "
                    "也可以读目录: 给目录路径返回排序后的文件名列表."),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录路径"},
            "offset": {"type": "integer", "description": "起始行号(1 起始), 默认 1"},
            "limit": {"type": "integer", "description": "最多读多少行, 默认 2000"},
        },
        "required": ["path"],
    },
    "risk": "read",
    "run": run_read,
}


# ------ Tool: write / edit ------
def _backup(path):
    """写前备份(path.bak, 已有 .bak 就覆盖). 家法: 改文件永远留退路."""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
        return path + ".bak"
    return None


def run_write(args, ctx):
    orig = args.get("path") or args.get("file_path") or ""
    path = _resolve(ctx, orig)
    content = args.get("content")
    if not path:
        return ToolRet(False, "missing 'path'")
    if _is_secret_store(path):
        return ToolRet(False, "不许写本地存储(keys/模型库/配置) —— 防篡改 key 入口; "
                              "要改配置请在界面里操作.")
    if not isinstance(content, str):
        return ToolRet(False, "missing 'content' (string)")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    bak = _backup(path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    note = "旧文件已备份 %s.bak" % orig if bak else "新建文件"
    return ToolRet(True, "已写入 %s (%d 字符, %s)" % (orig, len(content), note))


def run_edit(args, ctx):
    orig = args.get("path") or args.get("file_path") or ""
    path = _resolve(ctx, orig)
    old = args.get("old_string")
    new = args.get("new_string", "")
    if not path:
        return ToolRet(False, "missing 'path'")
    if not isinstance(old, str) or not old:
        return ToolRet(False, "missing 'old_string' (非空 string, 要精确唯一匹配的原文)")
    if not isinstance(new, str):
        return ToolRet(False, "'new_string' 必须是 string")
    if old == new:
        return ToolRet(False, "old_string 与 new_string 相同, 无需编辑")
    if _is_secret_store(path):
        return ToolRet(False, "不许改本地存储(keys/模型库/配置) —— 防篡改 key 入口; "
                              "要改配置请在界面里操作.")
    if not os.path.isfile(path):
        return ToolRet(False, "no such file: %s" % orig)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        return ToolRet(False, "%s 不是 utf-8 文本, 拒绝盲改" % orig)
    except OSError as ex:
        return ToolRet(False, "read failed: %s" % ex)
    count = text.count(old)
    if count == 0:
        return ToolRet(False, "old_string 在 %s 中找不到(要求精确匹配含空白). 先 read 看原文再改." % orig)
    if count > 1:
        return ToolRet(False, "old_string 命中 %d 处, 必须唯一; 加长上下文再试" % count)
    _backup(path)
    new_text = text.replace(old, new, 1)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    line_no = new_text[:new_text.index(new)].count("\n") + 1 if new else text[:text.index(old)].count("\n") + 1
    return ToolRet(True, "已替换 %s 第 %d 行附近 (%d 字符 → %d 字符, 旧文件已备份 .bak)"
                   % (orig, line_no, len(old), len(new)))


WRITE = {
    "name": "write",
    "description": ("把整份内容写入文件(utf-8, 覆盖式). 覆盖已有文件前自动备份为 <path>.bak. "
                    "适合新建文件或整文件重写; 局部修改请用 edit(精确替换, 更省 token). "
                    "相对路径相对当前工作目录."),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    },
    "risk": "write",
    "run": run_write,
}

EDIT = {
    "name": "edit",
    "description": ("在文件里做一处精确文本替换: old_string 必须与原文完全一致(含缩进/空白)且全文件唯一, "
                    "命中 0 处或多处都会报错并说明. 改前自动备份 .bak. "
                    "改代码片段/配置行用它, 别整文件重写."),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文(精确匹配, 须唯一)"},
            "new_string": {"type": "string", "description": "替换成的文本(空串=删除)"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    "risk": "write",
    "run": run_edit,
}


# ------ Tool: grep / glob ------
SKIP_DIRS = {"run", "__pycache__", ".git", "node_modules", ".venv", "dist"}
SKIP_SUFFIX = (".exe", ".dll", ".png", ".jpg", ".zip", ".pyc", ".pdf", ".xlsx")


_BASE = os.path.dirname(os.path.abspath(__file__))


def _is_secret_store(path):
    """"存 api 的地方"判定 —— 读工具家族硬拒(不弹框): key 绝不进任何档的家法扩围.
    2026-10-13 双实锤: ①客制化 agent 用 read 把 keys.json 明文读进会话档;
    ②用户拍板"严防中转站的模型盗用"—— 内容一旦进了对话, 就会随请求发到中转站
    第三方通道的日志里. 覆盖: 任意 .agent-term 目录树(keys/models/settings/记忆/
    版本库) + providers.json 两处(仓库级与用户主目录级; 可能被手填内联 key)."""
    p = os.path.abspath(path or "").replace("\\", "/").lower()
    if "/.agent-term/" in p or p.endswith("/.agent-term"):
        return True
    for pj in (os.path.join(os.path.expanduser("~"), "providers.json"),
               os.path.join(_BASE, "providers.json")):
        if p == pj.replace("\\", "/").lower():
            return True
    return False


def _cites_secret_store(cmd):
    """bash 命令文本扫描: 提到存储点就不许跑(严防绕道读 key; 宁可误伤也别漏)."""
    c = (cmd or "").lower()
    return any(s in c for s in (".agent-term", "keys.json", "providers.json",
                                "models.json"))


def _iter_files(root, glob_filter=None):
    """遍历 root 下文本文件(跳过 SKIP_DIRS/二进制后缀/大文件), 或 root 本身是文件时就它一个."""
    if os.path.isfile(root):
        if _is_secret_store(root):
            return                       # 存储点不参与 grep 内容遍历(见 _is_secret_store)
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(SKIP_SUFFIX):
                continue
            if glob_filter and not fnmatch.fnmatch(fn, glob_filter):
                continue
            full = os.path.join(dirpath, fn)
            if _is_secret_store(full):
                continue
            try:
                if os.path.getsize(full) > 4 * 1024 * 1024:
                    continue
            except OSError:
                continue
            yield full


def run_grep(args, ctx):
    pattern = args.get("pattern") or ""
    if not pattern:
        return ToolRet(False, "missing 'pattern' (正则)")
    root = _resolve(ctx, args.get("path") or ".")
    glob_filter = args.get("glob") or None
    ignore_case = bool(args.get("ignore_case", False))
    try:
        max_results = min(int(args.get("max_results", 200)), 1000)
    except (TypeError, ValueError):
        max_results = 200
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as ex:
        return ToolRet(False, "正则错误: %s" % ex)
    if not os.path.exists(root):
        return ToolRet(False, "no such path: %s" % root)
    if _is_secret_store(root):
        return ToolRet(False, "不许搜本地存储(keys/模型库/配置) —— 防 key 经对话"
                              "外泄给中转通道; 要看配置请在界面里操作.")
    base = os.path.dirname(root) if os.path.isfile(root) else root
    hits, truncated = [], False
    for full in _iter_files(root, glob_filter):
        rel = os.path.relpath(full, base)
        try:
            with open(full, "rb") as f:
                if b"\x00" in f.read(1024):
                    continue  # 二进制
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append("%s:%d:%s" % (rel, i, line.strip()[:200]))
                        if len(hits) >= max_results:
                            truncated = True
                            break
        except OSError:
            continue
        if truncated:
            break
    if not hits:
        return ToolRet(True, "(无匹配: pattern=%s root=%s)" % (pattern, root))
    return ToolRet(True, "\n".join(hits), truncated)


def run_glob(args, ctx):
    pattern = args.get("pattern") or ""
    if not pattern:
        return ToolRet(False, "missing 'pattern' (如 '*.py' 或 '**/*.md')")
    root = _resolve(ctx, args.get("path") or ".")
    if not os.path.isdir(root):
        return ToolRet(False, "no such dir: %s" % root)
    rels = []
    for m in globmod.glob(os.path.join(root, pattern), recursive=True):
        rel = os.path.relpath(m, root)
        parts = rel.split(os.sep)
        if any(p in SKIP_DIRS for p in parts[:-1]):   # 跳过 run/.git 等, 与 grep 同规矩
            continue
        rels.append(rel)
    rels.sort()
    if not rels:
        return ToolRet(True, "(无匹配: %s)" % pattern)
    clipped, truncated = _clip("\n".join(rels), head=500, tail=0, max_chars=32000)
    return ToolRet(True, clipped, truncated or len(rels) > 500)


GREP = {
    "name": "grep",
    "description": ("在目录(或单文件)里按正则搜文本, 返回 文件:行号:内容. 默认搜当前工作目录, "
                    "可用 glob 参数过滤文件名(如 '*.py'), ignore_case 忽略大小写. "
                    "自动跳过 run/__pycache__/.git/二进制/大文件. 最多返回 200 条(可调)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索根路径, 默认当前目录"},
            "glob": {"type": "string", "description": "文件名过滤, 如 '*.py'"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写, 默认 false"},
            "max_results": {"type": "integer", "description": "最大条数, 默认 200, 上限 1000"},
        },
        "required": ["pattern"],
    },
    "risk": "read",
    "run": run_grep,
}

GLOB = {
    "name": "glob",
    "description": "按通配符列文件路径, 如 '*.py'(当前层) 或 '**/*.md'(递归). 返回排序后的相对路径列表.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "通配符, 支持 ** 递归"},
            "path": {"type": "string", "description": "根目录, 默认当前目录"},
        },
        "required": ["pattern"],
    },
    "risk": "read",
    "run": run_glob,
}


# ------ Tool: todo ------
TODO_STATUS = ("pending", "in_progress", "done")
TODO_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


def render_todos(items):
    """任务清单的可读形态(工具回喂与 TUI 渲染共用同一份, 口径永不漂移)."""
    return "\n".join("%d. %s %s" % (i + 1, TODO_MARK.get(x["status"], "[?]"), x["content"])
                     for i, x in enumerate(items))


def run_todo(args, ctx):
    """全量写式任务清单(对齐 Claude Code 的 TodoWrite): 每次提交完整列表, 不做增量."""
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return ToolRet(False, "missing 'items' (非空数组, 每项 {content, status})")
    if len(items) > 50:
        return ToolRet(False, "items 最多 50 条, 拆分任务或合并条目")
    clean = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return ToolRet(False, "items[%d] 不是对象" % i)
        content = it.get("content")
        if not isinstance(content, str) or not content.strip():
            return ToolRet(False, "items[%d].content 缺失或为空" % i)
        status = it.get("status", "pending")
        if status not in TODO_STATUS:
            return ToolRet(False, "items[%d].status=%r 非法, 只许 %s"
                           % (i, status, "/".join(TODO_STATUS)))
        clean.append({"content": content.strip()[:200], "status": status})
    ctx.todos = clean
    if ctx.bus is not None:
        ctx.bus.emit("todo_update", items=clean)   # TUI/实验采集器可订阅
    if ctx.session is not None:
        try:
            ctx.session.append("todo", clean)      # M7 断电续传: 全量写式落 transcript
        except Exception:
            pass
    return ToolRet(True, "任务清单已更新(%d 条):\n%s" % (len(clean), render_todos(clean)))


TODO = {
    "name": "todo",
    "description": ("维护本次会话的任务清单: 每次提交完整列表(全量覆盖, 非增量). "
                    "多步任务开工前先列清单, 每完成一步把对应条目 status 改 done 并回传全表, "
                    "让用户随时可见进度. 状态只许 pending/in_progress/done."),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "完整任务列表, 每项 {content: 任务描述, status: pending|in_progress|done}",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["items"],
    },
    "risk": "read",   # 纯会话内状态, 不碰文件系统, 无需确认
    "run": run_todo,
}


# ------ Tool: memory(M9 记忆卡; 2026-10-20 起只存不读) ------
def run_memory(args, ctx):
    """跨会话共享记忆卡: list=列卡名+摘要(写卡查重/提醒用户注入用). 读内容不在这
    工具里 —— 用户拍板"忆库只管存, 要用的时候在聊天里插卡": 卡内容只有用户点
    「注入」进对话才可见. 只读, 免权限门."""
    import memory as memory_mod
    action = args.get("action", "list")
    if action == "list":
        cards = memory_mod.list_cards()          # 两区合并, 每张带 zone 标
        if not cards:
            return ToolRet(True, "记忆库是空的(没有记忆卡).")
        lines = ["共 %d 张记忆卡(新→旧):" % len(cards)]
        for c in cards:
            mark = "〔遗忘区·周末清理〕" if c.get("zone") == "forget" else ""
            lines.append("- %s%s (%s · %dB): %s"
                         % (c["title"], mark, memory_mod.fmt_mtime(c["mtime"]),
                            c["size"], c["head"][:50]))
        return ToolRet(True, "\n".join(lines))
    if action == "read":
        return ToolRet(False, "忆库只存不读 —— 需要这张卡的内容时, 让用户在忆库页"
                              "点「注入」把卡插进对话(或提醒用户去注入), 你不能自己读.")
    return ToolRet(False, "action 只许 list, 收到 %r" % action)


MEMORY = {
    "name": "memory",
    "description": ("跨会话共享记忆卡清单(只存不读): action=list 列全部卡名+时间+摘要, "
                    "用于写卡查重, 或发现与本任务有关的卡时提醒用户到忆库页点「注入」. "
                    "卡内容不在这里读 —— 只有用户把卡注入对话后你才看得到. "
                    "写入/更新用 memory_write 工具."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list"],
                       "description": "list=列全部卡片(卡名+时间+摘要)"},
        },
        "required": [],
    },
    "risk": "read",
    "run": run_memory,
}


# ------ Tool: memory_write(M9: agent 自己存卡) ------
def _unique_update_title(title: str) -> str:
    """「X（更新）」/「X（更新2）」… 找第一个没被占的另存名(不覆盖模式用)."""
    import memory as memory_mod
    cand, n = "%s（更新）" % title, 2
    while memory_mod.card_exists(cand):
        cand = "%s（更新%d）" % (title, n)
        n += 1
    return cand


# 覆盖确认选项(2026-10-13 用户拍板"能问就问"): 同名覆盖必须过用户那关
_COVER_OPTIONS = (
    {"label": "覆盖更新", "description": "旧版自动留底(.bak), 忆库可找回"},
    {"label": "另存新卡", "description": "原卡不动, 新内容存成「同名（更新）」"},
    {"label": "取消", "description": "什么都不写"},
)


def run_memory_write(args, ctx):
    """把值得跨会话保留的信息存成记忆卡(新建; 同名覆盖先问用户). 发 memory_write
    事件 → 各窗卡列热刷. 写类: 过权限门, 用户可'本会话总允许'.
    覆盖双轨(用户拍板): 有选择界面 → 弹卡问[覆盖更新/另存新卡/取消]; 无界面
    (终端/批跑) → 照写但在结果里显著注明"无询问通道, 已直接覆盖"."""
    import memory as memory_mod
    title = (args.get("title") or "").strip()
    content = (args.get("content") or "").strip()
    if not title:
        return ToolRet(False, "missing 'title' (卡名, 短而准)")
    if not content:
        return ToolRet(False, "missing 'content' (卡内容)")
    raw = args.get("merge_from")
    merge = ([str(x).strip() for x in raw if str(x or "").strip()][:5]
             if isinstance(raw, list) else [])
    existed = memory_mod.card_exists(title)
    v0 = memory_mod.card_version(title) if existed else ""   # 乐观并发基线(内容 sha1)
    cover_note = ""
    mo = ctx.memory_overwrite            # 设置页「同名覆盖策略」(live 可 callable)
    if callable(mo):
        try:
            mo = mo() or "ask"
        except Exception:
            mo = "ask"
    mo = mo or "ask"
    if existed:
        if mo == "auto":                 # 设置: 直接覆盖(用户明确选的, 不打扰)
            cover_note = "已按设置直接覆盖; "
        elif mo == "keep":               # 设置: 不覆盖 —— 同名一律另存
            title, existed, v0 = _unique_update_title(title), False, ""
            cover_note = "原卡未动, 同名不覆盖已另存为「%s」(设置); " % title
        else:                            # ask(默认): 能问就问
            asked = _ask_via_ui(ctx, "新内容和现有卡「%s」同名 —— 要覆盖旧卡吗?" % title,
                                "覆盖记忆卡", list(_COVER_OPTIONS)) \
                if ctx.ask_user is not None else None
            if asked is None:
                cover_note = "⚠ 当前无询问通道(终端/批跑), 已直接覆盖; "
            else:
                if asked["skipped"]:
                    return ToolRet(True, "用户跳过了覆盖确认 —— 未写入任何卡"
                                         "(要记的话换个卡名或先说清).")
                if asked["custom"]:
                    return ToolRet(True, "用户没选预设, 而是说: %s —— 未写入任何卡; "
                                         "请按用户的意思处理后重试." % asked["custom"])
                if asked["label"] == "取消":
                    return ToolRet(True, "用户取消了这次覆盖 —— 未写入任何卡.")
                if asked["label"] == "另存新卡":
                    title, existed, v0 = _unique_update_title(title), False, ""
                    cover_note = "原卡未动, 另存为「%s」(用户选择); " % title
                # 其余(覆盖更新): 正常往下走
    t, why = memory_mod.write_card_guarded(title, content, expect_version=v0)
    if why == "badname":
        return ToolRet(False, "卡名清洗后为空(去掉 \\ / : * ? \" < > | 与换行再试)")
    if why == "raced":
        return ToolRet(False, "这张卡在确认/生成期间被别处改过(另一个窗口或轮末自动记忆"
                              "刚动过它) —— 为避免覆盖它的新内容, 本次未写。请先 memory read "
                              "看最新版再决定, 或换个卡名另存。")
    merged, missing = [], []
    for src in merge:
        if src == t:
            continue                       # 源=目标: 本身就是覆写, 无合并可言
        z = memory_mod.find_zone(src)
        if not z:
            missing.append(src)
            continue
        if memory_mod.delete_card(src, zone=z, keep_bak=True):
            merged.append(src)
        else:
            missing.append(src)
        # 源卡在遗忘区也一并合并(卡名两区搜, 留底同规矩)
    extra = {"merged_from": merged} if merged else {}
    if ctx.bus is not None:
        ctx.bus.emit("memory_write", title=t, overwritten=existed, src="agent", **extra)
    note = ("同名覆写, 旧版已留 .bak" if existed else "新建")
    out = "记忆卡已保存(%s): %s\n%s所有会话可用 memory 工具读取, " \
          "用户在各聊天窗左侧可见这张卡。" % (note, t, cover_note)
    if merged:
        out += "\n已合并 %d 张(源卡已留 .bak, 反悔可恢复): %s" % (len(merged), "、".join(merged))
    if missing:
        out += "\n(这些卡名没找到, 未动: %s)" % "、".join(missing)
    return ToolRet(True, out)


MEMORY_WRITE = {
    "name": "memory_write",
    "description": ("把值得跨会话保留的信息存成记忆卡(所有会话/用户可见): 用户偏好与"
                    "长期指示 / 项目背景与关键决定 / 工具核实过的稳定事实. "
                    "卡名短而准; 只存对未来会话真正有用的"
                    "事实, 不存临时上下文和一次性的任务细节. "
                    "防重复规矩: 写卡前先 memory list; 若与且仅与一张现有卡同主题 → 直接"
                    "用那张卡的原标题写(同名覆盖会自动弹卡让用户确认, 你不用自己再问覆盖); "
                    "若与多张卡重叠、或本轮要写多张彼此重叠的卡 → 先用 ask_user 问用户"
                    "(只更新最初那张 / 合并为一新卡 / 都保留), 选合并时把源卡名列进 "
                    "merge_from(源卡自动留 .bak 可恢复)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "卡名(唯一; 同名覆写)"},
            "content": {"type": "string", "description": "卡内容: 给未来会话读的事实, 直接可读的文本"},
            "merge_from": {"type": "array", "items": {"type": "string"},
                           "description": "可选(合并用): 要把内容并进来的源卡名列表(≤5). "
                                          "写卡成功后这些卡被删除但留 .bak 可恢复."},
        },
        "required": ["title", "content"],
    },
    "risk": "write",
    "run": run_memory_write,
}


# ------ Tool: ask_user(选项提问; M-k2) ------
def _ask_via_ui(ctx, question, header, options):
    """走 UI 通道问选择题的统一封装(事件+计时): 返回规范答案 {"label","custom","skipped"};
    ctx.ask_user 为 None(REPL/批跑/无头) 返回 None. ask_user 工具与 memory_write
    的覆盖确认共用这一份 —— 口径不漂移."""
    if ctx.ask_user is None:
        return None
    if ctx.bus is not None:
        ctx.bus.emit("ask_user", question=question, header=header, options=options)
    t0 = time.time()
    ans = ctx.ask_user(question, header, options)
    seconds = round(time.time() - t0, 2)
    if isinstance(ans, dict):              # {"label","custom","skipped"} 规范形
        label = str(ans.get("label") or "").strip()
        custom = str(ans.get("custom") or "").strip()
        skipped = bool(ans.get("skipped")) or (not label and not custom)
    else:                                  # 宽容: 纯字符串当 label
        label = str(ans or "").strip()
        custom, skipped = "", not label
    if ctx.bus is not None:
        ctx.bus.emit("ask_user_answered", label=label, custom=custom,
                     skipped=skipped, seconds=seconds)
    return {"label": label, "custom": custom, "skipped": skipped}


def run_ask_user(args, ctx):
    """给用户出选择题: 2~4 个选项(label/description/preview), 聊天窗渲染成卡片,
    点选结果作为本工具返回值回喂模型. 无选择界面(REPL/批跑/无头) → error ToolRet
    让模型改用文字提问 —— 绝不阻塞(实验/无人值守永不被人挂住).
    发 ask_user / ask_user_answered 事件(决策延迟秒数进 trace=研究口)."""
    question = (args.get("question") or "").strip()
    options = args.get("options")
    if not question:
        return ToolRet(False, "ask_user: 缺 question(要问用户什么?)")
    if not isinstance(options, list) or not (2 <= len(options) <= 4):
        return ToolRet(False, "ask_user: options 要给 2~4 个(收到 %s 个)"
                       % (len(options) if isinstance(options, list) else "非数组"))
    clean = []
    for i, o in enumerate(options):
        if not isinstance(o, dict) or not str(o.get("label") or "").strip():
            return ToolRet(False, "ask_user: 第 %d 个选项缺 label" % (i + 1))
        clean.append({"label": str(o.get("label")).strip()[:40],
                      "description": str(o.get("description") or "").strip()[:200],
                      "preview": str(o.get("preview") or "")[:2000]})
    if ctx.ask_user is None:
        return ToolRet(False, "当前环境没有图形选择界面(REPL/批跑/无头) —— "
                              "请直接用文字向用户提问并给出选项, 不要反复调本工具.")
    header = str(args.get("header") or "").strip()[:20]
    ans = _ask_via_ui(ctx, question, header, clean)
    if ans["skipped"]:
        return ToolRet(True, "用户跳过了这个问题(没有选择) —— 请自行拿主意, 别反复追问.")
    if ans["custom"]:
        return ToolRet(True, "用户选择:(自填) %s" % ans["custom"])
    return ToolRet(True, "用户选择: %s" % ans["label"])


ASK_USER = {
    "name": "ask_user",
    "description": ("给用户出选择题(方案分叉/取舍/规格不明确时用): 给 2~4 个选项, "
                    "每项 label 短句 + 可选 description 说明 + 可选 preview(等宽文本, "
                    "如布局草图). 用户在界面上点选, 结果作为本工具返回值给你; 界面自带"
                    "「其它(自己输入)」和跳过. 用户可能跳过 —— 那就自己拿主意继续."),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题(完整一句)"},
            "header": {"type": "string", "description": "不超过 12 字的短标签(可空)"},
            "options": {
                "type": "array",
                "description": "2~4 个选项",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "选项短名(≤5 词)"},
                        "description": {"type": "string", "description": "这个选项意味什么"},
                        "preview": {"type": "string",
                                    "description": "可选: 等宽文本预览(布局草图/代码片段)"},
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["question", "options"],
    },
    "risk": "read",   # 纯交互不碰文件; 等待由 UI 侧可中断(中断按钮解锁等待)
    "run": run_ask_user,
}


# ------ Tool: plan_submit(M24 计划模式: 计划提交审批) ------

def run_plan_submit(args, ctx):
    """计划模式(M24)的收口: 把完整计划提交用户审批. 复用 ask_user 的卡与桥
    (三选项 + 计划全文进 preview); 批准/取消就地翻 ctx.perms.mode —— 计划本体
    在工具调用参数里, 模型自己可见, 不再额外注入消息."""
    plan = str(args.get("plan") or "").strip()
    if not plan:
        return ToolRet(False, "plan_submit: 缺 plan(完整计划: 目标/步骤/涉及文件/风险)")
    if len(plan) > 20000:
        plan = plan[:20000] + "\n…(超长截断)"
    options = [{**o, "preview": plan} for o in (
        {"label": "批准执行", "description": "退出计划模式, 按计划开工"},
        {"label": "继续完善", "description": "留在计划模式, 按备注改计划再提交"},
        {"label": "取消", "description": "退出计划模式, 本任务不做"},
    )]
    ans = _ask_via_ui(ctx, "计划已提交, 请审批(全文见预览)", "计划审批", options)
    if ans is None:
        return ToolRet(False, "当前环境没有审批界面(REPL/批跑/无头) —— 计划模式只在"
                              "图形聊天窗可用; 请直接用文字向用户陈述计划.")

    def set_mode(m):
        if getattr(ctx, "perms", None) is not None:
            ctx.perms.mode = m

    if ans["skipped"]:
        return ToolRet(True, "用户跳过了审批 —— 保持计划模式, 用文字向用户简述计划"
                             "要点等明确答复, 不要自行开工.")
    if ans["custom"]:
        return ToolRet(True, "用户批复(未点选项): %s —— 保持计划模式, 按批复修改后"
                             "重新调 plan_submit 提交." % ans["custom"])
    if ans["label"] == "批准执行":
        set_mode("normal")
        return ToolRet(True, "计划已批准, 已退出计划模式 —— 按计划逐步执行(全文就在你"
                             "刚才的调用参数里), 做完收尾汇报.")
    if ans["label"] == "取消":
        set_mode("normal")
        return ToolRet(True, "用户取消并退出计划模式 —— 本任务到此为止, 简短确认后等"
                             "用户新指示.")
    return ToolRet(True, "用户要求继续完善(备注: %s) —— 仍处计划模式, 修改计划后重新"
                         "调 plan_submit." % (ans["label"] or ""))


PLAN_SUBMIT = {
    "name": "plan_submit",
    "description": ("计划模式(用户在聊天窗开「计划」胶囊)的收口: 先用只读工具调研, 把完整计划"
                    "(目标/步骤/涉及文件/风险)通过本工具提交, 用户在卡片上审批 —— 批准后自动"
                    "退出计划模式开工. 计划模式里写/执行类会被拒, 这是设计不是故障; 别在非"
                    "计划模式下调用."),
    "input_schema": {
        "type": "object",
        "properties": {
            "plan": {"type": "string", "description": "完整计划文本"},
        },
        "required": ["plan"],
    },
    "risk": "read",   # 纯交互; 等待由 UI 侧可中断
    "run": run_plan_submit,
}


# ------ Tool: platform(直控 agent-term 自身界面; M13.2) ------
_PLATFORM_PAGES = ("chat", "models", "memlib", "factory", "skills", "ledger",
                   "voice", "custom", "rollback", "settings")


def run_platform(args, ctx):
    """操作 agent-term 自身界面: goto=主窗跳模块页; gen_pipeline=一句话生成流水线
    并自动放进组装厂画布(生成后自动跳过去). 仅在有图形界面的会话(聊天窗)可用;
    REPL/批跑/客制化 agent 无桥 → error 引导文字指路, 绝不阻塞."""
    if ctx.platform is None:
        return ToolRet(False, "当前环境没有图形界面可操作(REPL/批跑/无头) —— 请改用"
                              "文字向用户指路(如: 组装厂页面底部有「智能组装」抽屉).")
    action = (args.get("action") or "").strip()
    if action == "goto":
        page = (args.get("page") or "").strip()
        if page not in _PLATFORM_PAGES:
            return ToolRet(False, "goto: page 必须是 %s 之一(收到 %r)"
                           % ("/".join(_PLATFORM_PAGES), page))
        params = {"page": page}
    elif action == "gen_pipeline":
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return ToolRet(False, "gen_pipeline: 缺 prompt(要生成什么样的流水线)")
        params = {"prompt": prompt[:2000], "model": (args.get("model") or "").strip()}
    else:
        return ToolRet(False, "platform: action 只支持 goto / gen_pipeline(收到 %r)" % action)
    t0 = time.time()
    try:
        res = ctx.platform(action, **params)
    except Exception as ex:                # 桥抛异常不炸整轮(工具错误回喂铁律)
        res = {"ok": False, "err": "界面操作异常: %s: %s" % (type(ex).__name__, ex)}
    seconds = round(time.time() - t0, 2)
    ok = bool(isinstance(res, dict) and res.get("ok"))
    text = ""
    if isinstance(res, dict):
        text = str(res.get("result") or res.get("err") or "").strip()
    elif res is not None:
        text = str(res).strip()
    if ctx.bus is not None:
        ctx.bus.emit("platform_action", action=action, params=params, ok=ok,
                     seconds=seconds)   # trace 研究口(含操作耗时)
    return ToolRet(ok, text or ("完成" if ok else "操作失败(界面侧没给原因)"))


PLATFORM = {
    "name": "platform",
    "description": ("直接操作 agent-term 自身界面(用户要你打开某个页面/要拼流水线时用它, "
                    "别只口头指路): goto=把主窗口跳到指定模块页; gen_pipeline=按一句话"
                    "生成 agent 流水线并自动放进组装厂画布(会自动跳过去, 用户可继续手工调). "
                    "只在有图形界面的会话里可用; 若返回'没有图形界面', 再改用文字指路."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["goto", "gen_pipeline"]},
            "page": {"type": "string", "enum": list(_PLATFORM_PAGES),
                     "description": "goto 必填: chat=会话 / models=模型 / memlib=忆库 / "
                                    "factory=组装厂 / skills=技能 / ledger=账本 / "
                                    "voice=语音转文字 / custom=客制化 / rollback=回滚 / "
                                    "settings=设置"},
            "prompt": {"type": "string",
                       "description": "gen_pipeline 必填: 想要什么样的流水线(一句话描述)"},
            "model": {"type": "string",
                      "description": "gen_pipeline 可选: 用哪个可调用 agent 生成"
                                     "(如 glm/glm-5.2); 默认跟当前选家"},
        },
        "required": ["action"],
    },
    "risk": "read",   # 纯界面操作; 生成与点击抽屉「生成产线」等价(用户既定管线)
    "run": run_platform,
}


# ------ Tool: spawn(分身/子代理; M14) ------
SUB_LABEL_CAP = 16   # 分身标签字符上限(前端 ⤷ 行显示用)


def run_spawn(args, ctx):
    """派一个分身子代理去干活. 本层只做入参清洗; 深度守卫/模型解析/子循环编排全在
    loop 侧(ctx.spawn 绑方法) —— 工具层不知道 AgentLoop 的存在."""
    if ctx.spawn is None:
        return ToolRet(False, "本环境未启用分身(没有可派的子代理) —— 自己把活干完.")
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return ToolRet(False, "spawn: 缺 prompt(要给分身派什么活?)")
    label = re.sub(r"\s+", " ", str(args.get("label") or "")).strip()[:SUB_LABEL_CAP]
    model = str(args.get("model") or "").strip()
    agent_type = str(args.get("agent_type") or "").strip() or "general"
    if agent_type not in ("general", "explore"):
        return ToolRet(False, "spawn: agent_type 只认 general(全能)/explore(只读探查), "
                              "收到 %r" % agent_type)
    inherit = args.get("inherit")
    inherit = 0 if inherit is None else inherit
    try:
        inherit = int(inherit)
    except (TypeError, ValueError):
        return ToolRet(False, "spawn: inherit 要是 0~40 的整数(带给分身的最近对话条数)")
    if not (0 <= inherit <= 40):
        return ToolRet(False, "spawn: inherit 范围 0~40")
    images = args.get("images")            # M32: 识图分身 —— 图路径随任务进子对话
    if images is not None:
        if not isinstance(images, list) or not all(isinstance(x, str) for x in images):
            return ToolRet(False, "spawn: images 要是字符串数组(本地图片文件路径)")
        images = [p.strip() for p in images if str(p).strip()] or None
    return ctx.spawn(prompt, label, model, agent_type=agent_type, inherit=inherit,
                     images=images)


SPAWN = {
    "name": "spawn",
    "description": (
        "派一个分身子代理去干活(多路探查/互相独立的子任务/多方交叉验证): 一条消息里发"
        "多个 spawn = 并行执行(最多 4 个), 每个分身的结论作为本工具的返回值回到你手里, "
        "由你汇总. 派活时把任务写清楚并说清要它带回什么结论(证据/路径/数字) —— 它看不到"
        "你和用户的对话, 也不会中途问你. 简单任务别用(自己直接做更快)."
        "带 images = 识图分身: 图会直接喂给分身用的模型看, 须挑视觉档(「模型」页勾了"
        "「能看图」的条目, 如 qwen-vl / glm-4v-plus) —— 一次最多 2 张."),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "派给分身的完整任务(自包含: 它看不到我们的对话)"},
            "label": {"type": "string",
                      "description": "可选: 短标签(≤16字), 用户界面里显示这是哪个分身"},
            "model": {"type": "string",
                      "description": "可选: 给分身挑个别的可调用 agent(家/型号, 如 "
                                     "glm/glm-5.2; 粗活可以派更划算的档); 默认=当前模型; "
                                     "需用户先在设置里允许「分身用非当前模型」; 仅前端聊天窗支持"},
            "agent_type": {"type": "string",
                           "description": "可选: general=全能(默认) / explore=只读探查"
                          "(没有 write/edit, 只调研取证 —— 搜代码/读文件/查网页类任务)"},
            "inherit": {"type": "integer",
                        "description": "可选: 带主对话最近 N 条(user/assistant)给分身当"
                                       "背景(0=不带, 默认; 一般不需要, 任务写自包含即可)"},
            "images": {"type": "array", "items": {"type": "string"},
                       "description": "可选: 本地图片文件路径(最多 2 张) —— 识图分身: "
                                      "图喂给分身用的模型看, 须挑视觉档; 当前模型看不了"
                                      "图时走这里"},
        },
        "required": ["prompt"],
    },
    "risk": "read",   # 起代理本身不碰文件; 分身内层各工具照吃同一个权限门(三键确认不变)
    "run": run_spawn,
}


# ------ Tool: look_image(识图单发; M32; 2026-10-20 起支持 GIF/视频) ------
def run_look_image(args, ctx):
    """壳: 校验入参 → loop 侧解析视觉档并单轮调用(本地图/GIF/视频→结论文字)."""
    if ctx.look_image is None:
        return ToolRet(False, "本环境未启用识图 —— 让用户在「模型」页给条目勾上"
                              "「能看图」, 或换个环境跑.")
    path = str(args.get("path") or "").strip()
    if not path:
        return ToolRet(False, "look_image: 缺 path(哪张图? 本地图片/GIF/视频文件路径)")
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolRet(False, "look_image: 缺 question(要看什么? 写具体: 读哪块文字/"
                              "数什么/对比什么)")
    model = str(args.get("model") or "").strip()
    try:
        frames = max(1, min(int(args.get("frames") or 2), 2))
    except (TypeError, ValueError):
        frames = 2
    return ctx.look_image(path, question, model, frames)


LOOK_IMAGE = {
    "name": "look_image",
    "description": (
        "看本地图片/GIF/视频, 回传文字结论(单次视觉调用, 不带工具不多轮): 适合读截图"
        "文字/看图表/认界面元素这类一眼活. 支持 png/jpg/webp/gif(动图自动抽帧)/常见"
        "视频 mp4·webm·mov 等(抽帧+时长分辨率元数据, 需服务器装了 ffmpeg). 结果自动"
        "附原图进聊天流 —— 用户能直接看到图, 你不用再想办法发图. 复杂活(看完"
        "图还要查文件/多轮追问/对比多图)改用 spawn 带 images 派识图分身. path 传本地"
        "文件路径(截图/用户拖进来的图); 当前模型不是视觉档时点名 model=视觉档(家/型号),"
        " 或改走 spawn."),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "本地图片/GIF/视频文件路径(png/jpg/webp/gif/"
                                    "mp4/webm/mov 等)"},
            "question": {"type": "string",
                         "description": "要看什么(写具体: 读哪块文字/数什么/对比什么)"},
            "model": {"type": "string",
                      "description": "可选: 点名视觉档模型(家/型号); 当前模型是视觉档"
                                     "可省略; 受「分身用非当前模型」开关管"},
            "frames": {"type": "integer",
                       "description": "可选: GIF 动图/视频抽几帧(1-2, 默认 2)"},
        },
        "required": ["path", "question"],
    },
    "risk": "read",   # 只读图/视频抽帧+一次推理; 跨模型花钱留账本(M19)
    "run": run_look_image,
}


# ------ Tool: image_gen(画图: 文生图/图生图; M15) ------
def run_image_gen_tool(args, ctx):
    """壳: 校验入参 → loop 侧解析画图档并调用(vision.run_image_gen + 事件)."""
    if ctx.image_gen is None:
        return ToolRet(False, "本环境没配画图模型 —— 去「模型」页新增/编辑一个条目并勾选"
                              "「能画图」(官方家如智谱 cogview, 或中转站填 base_url/key),"
                              "再来调用.")
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return ToolRet(False, "image_gen: 缺 prompt(要画什么?)")
    image = (args.get("image") or "").strip()
    if image and not os.path.isfile(image):
        return ToolRet(False, "image_gen: 找不到输入图 %r(图生图传本地图片路径)" % image)
    model = (args.get("model") or "").strip()
    return ctx.image_gen(prompt, image, model)


IMAGE_GEN = {
    "name": "image_gen",
    "description": (
        "生成图片(文生图/图生图): 给一句具体描述画一张图; 传 image(本地图片路径)则按描述"
        "改这张图(图生图, 需所配画图档型号支持图像编辑, 不支持会报错). 生成的图存到本地"
        "并显示在聊天流里, 后续回合视觉档模型也能再次看到它. 需用户在「模型」页给某个"
        "条目勾过「能画图」才可用 —— 报错说没配就转告用户去勾."),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "要画什么/怎么改(主体/风格/画面尽量具体)"},
            "image": {"type": "string",
                      "description": "可选: 本地图片路径 —— 传了就是图生图/图像编辑"},
            "model": {"type": "string",
                      "description": "可选: 点名哪个画图档(家/型号); 默认用第一个勾了「能画图」的"},
        },
        "required": ["prompt"],
    },
    "risk": "read",   # 生成花钱但不碰文件/系统, 同 spawn 先例
    "run": run_image_gen_tool,
}


# ------ Tool: skill(M17 技能包) ------
def run_skill(args, ctx):
    """技能包(打包好的操作手册): list=列名字+一句描述+来源; load=读全文注入上下文
    (发 skill_read 事件落 trace). 只读免权限门; 技能是静态规程不是隐私数据, 不弹确认
    (与记忆卡不同)."""
    import skills as skills_mod
    action = args.get("action", "list")
    if action == "list":
        items = skills_mod.list_skills()
        if not items:
            return ToolRet(True, "没有可用技能(仓库 skills/ 与 ~/.agent-term/skills/ "
                                 "都是空的; 技能页可新建).")
        lines = ["共 %d 个技能:" % len(items)]
        for s in items:
            mark = "〔个人·覆盖仓库版〕" if s["source"] == "user" else ""
            shadow = "〔仓库同名被个人覆盖〕" if s["shadowed"] else ""
            lines.append("- %s%s%s: %s" % (s["name"], mark, shadow, s["description"]))
        return ToolRet(True, "\n".join(lines))
    if action == "load":
        name = (args.get("name") or "").strip()
        body, source = skills_mod.read_skill(name)
        if not body:
            cands = [s["name"] for s in skills_mod.list_skills()]
            return ToolRet(False, "没有这个技能: %r (先 action=list 看清单; 现有: %s)"
                           % (name, ", ".join(cands) or "无"))
        if ctx.bus is not None:
            ctx.bus.emit("skill_read", name=name, source=source)   # trace: 技能消费可回放
        return ToolRet(True, "【技能·%s】(来源: %s)\n%s" % (name, source, body))
    return ToolRet(False, "action 只许 list/load, 收到 %r" % action)


SKILL = {
    "name": "skill",
    "description": ("技能包(打包好的操作手册, 系统提示词「可用技能」清单里列了名字+用途; "
                    "仓库 skills/ 与个人 ~/.agent-term/skills/ 两层维护, 同名个人技能优先): "
                    "任务与某项技能对得上时, 先 action=list 看有哪些技能, 再 action=load "
                    "读全文照着做; 技能正文就是给你的工作规程, 执行仍走现有工具与权限门"),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "load"],
                       "description": "list=列全部技能(名字+一句描述+来源); load=读指定一份全文"},
            "name": {"type": "string",
                     "description": "技能名(action=load 时必填, 见 list 结果)"},
        },
        "required": ["action"],
    },
    "risk": "read",
    "run": run_skill,
}


# ------ Tool: ledger(M19 分包账本) ------
def run_ledger(args, ctx):
    """分包账本(客观账+私有评分): list=最近 N 条; query=按模型/会话过滤; total=按
    发起者→分身模型聚总(含均分/last, 挑人用). 只读免权限门; 客观项由平台在每笔
    分身收工时自动落账, 评分由 ledger_score 工具追加."""
    import ledger as ledger_mod
    action = args.get("action", "list")
    if action == "list":
        rows = ledger_mod.list_entries(
            limit=int(args.get("limit") or 20),
            sid=(args.get("sid") or "").strip(),
            model=(args.get("model") or "").strip())
        if not rows:
            return ToolRet(True, "账本是空的(还没有过任何分包记录).")
        lines = ["最近 %d 笔分包:" % len(rows)]
        for r in rows:
            sc = r.get("_score")
            lines.append("- [id %s] %s · %s → %s(%s) · %s · in %s/out %s · %s%s%s"
                         % (r.get("id") or "?", ledger_mod.fmt_ts(r.get("ts")),
                            r.get("parent"), r.get("sub"), r.get("model"),
                            (r.get("task") or "")[:28],
                            (r.get("tokens") or {}).get("prompt_tokens", 0),
                            (r.get("tokens") or {}).get("completion_tokens", 0),
                            "ok" if r.get("ok") else "FAIL",
                            (" · 分%s(%s)" % (sc["score"], sc["reason"][:24]))
                            if sc else "",
                            (" · " + (r.get("result_head") or "")[:30])
                            if r.get("result_head") else ""))
        return ToolRet(True, "\n".join(lines))
    if action == "query":
        rows = ledger_mod.list_entries(
            limit=int(args.get("limit") or 50),
            sid=(args.get("sid") or "").strip(),
            model=(args.get("model") or "").strip())
        if not rows:
            return ToolRet(True, "没有匹配的分包记录.")
        lines = ["匹配 %d 笔:" % len(rows)]
        for r in rows:
            sc = r.get("_score")
            lines.append("- [id %s] %s · %s → %s(%s) · %s · 任务: %s · 结论: %s%s"
                         % (r.get("id") or "?", ledger_mod.fmt_ts(r.get("ts")),
                            r.get("parent"), r.get("sub"), r.get("model"),
                            "ok" if r.get("ok") else "FAIL",
                            (r.get("task") or "")[:40],
                            (r.get("result_head") or "")[:40],
                            (" · 分%s: %s" % (sc["score"], sc["reason"][:30]))
                            if sc else ""))
        return ToolRet(True, "\n".join(lines))
    if action == "total":
        groups = ledger_mod.aggregate()
        if not groups:
            return ToolRet(True, "账本是空的.")
        lines = ["按 发起者→分身模型 聚总(挑人看均分/成功率/tokens):"]
        for g in groups:
            sc = (" · 均分%s(评%d次, last %s)" % (
                g["avg_score"], g["scores_n"], g["last_score"])) \
                if g["scores_n"] else " · 还没被评过分"
            lines.append("- %s → %s: %d 笔(ok %d) · in %s/out %s/total %s%s"
                         % (g["parent"], g["model"], g["calls"], g["ok"],
                            g["tokens"]["prompt_tokens"],
                            g["tokens"]["completion_tokens"],
                            g["tokens"]["total_tokens"], sc))
        return ToolRet(True, "\n".join(lines))
    return ToolRet(False, "action 只许 list/query/total, 收到 %r" % action)


# ------ Tool: ledger_score(M21 私有评分) ------
def run_ledger_score(args, ctx):
    """给一笔分包账追加私有评分(0-10+理由, α染色). 评分只进工具链(挑人用),
    账本页不显示. 写类: 过权限门."""
    import ledger as ledger_mod
    ok, err = ledger_mod.append_score(args.get("id"), args.get("score"),
                                      args.get("reason") or "")
    if not ok:
        return ToolRet(False, err)
    return ToolRet(True, "已入账: 该笔分包的私有评分 %s/10(理由: %s) —— "
                         "下次挑人时 ledger total 会看到均分."
                   % (int(args.get("score")), (args.get("reason") or "").strip()[:40]))


LEDGER_SCORE = {
    "name": "ledger_score",
    "description": ("给一笔已入账的分包打私有评分(0-10 分 + 一句理由, α染色): "
                    "先 ledger list 拿到那条账的 id, 再按完成质量/服从度/速度打分. "
                    "评分只进工具链(下回 ledger total 挑人看均分), 账本页不显示; "
                    "同一笔可以重复评(以最后一次为准). 只对真实发生的分包评, "
                    "评完把 id 记进你的汇报里."),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "要评的那条账的 id(ledger list 里 [id N])"},
            "score": {"type": "integer", "description": "0-10 整数评分"},
            "reason": {"type": "string", "description": "一句理由(为什么这个分)"},
        },
        "required": ["id", "score", "reason"],
    },
    "risk": "write",
    "run": run_ledger_score,
}


LEDGER = {
    "name": "ledger",
    "description": ("分包账本(跨会话的客观账, 平台在每笔分身收工时自动记): "
                    "action=list 看最近的分包记录(时间/发起者/分身/任务头/tokens/"
                    "成败/结论头); action=query 按 model 或 sid 过滤; action=total "
                    "按 发起者→分身模型 聚总笔数与 tokens. 派分身(或流水线节点派)前"
                    "先查历史账: 哪些模型干过什么、上次结果如何; 账本只有客观项, "
                    "没有私有评分. 只读, 模型不写账."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "query", "total"],
                       "description": "list=最近N条; query=按 model/sid 过滤; "
                                      "total=按模型聚总"},
            "limit": {"type": "integer",
                      "description": "最多多少条(默认 list=20, query=50)"},
            "model": {"type": "string",
                      "description": "可选: 分身模型子串过滤(如 deepseek)"},
            "sid": {"type": "string",
                    "description": "可选: 发起会话 id 子串过滤"},
        },
        "required": ["action"],
    },
    "risk": "read",
    "run": run_ledger,
}


# ------ Register ------
# ------ Tool: web(M22 联网; 网络与解析全在 web.py, 这里只做校验与门) ------

def run_web_fetch(args, ctx):
    import web as web_mod
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return ToolRet(False, "missing 'url' (要抓的网页地址)")
    try:
        timeout = float(args["timeout"]) if args.get("timeout") is not None \
            else web_mod.FETCH_TIMEOUT
    except (TypeError, ValueError):
        return ToolRet(False, "timeout 必须是数字(秒)")
    timeout = max(3.0, min(timeout, 60.0))
    result = web_mod.fetch(url, timeout=timeout)
    if result.get("redirect"):
        return ToolRet(True, "跨站重定向 → %s\n确认可信后, 用该地址重新调用 web_fetch."
                       % result["redirect"])
    if not result.get("ok"):
        return ToolRet(False, "web_fetch 失败: %s" % result.get("error", "未知错误"))
    head = []
    if result.get("title"):
        head.append("# " + result["title"])
    head.append("URL: %s" % result.get("final_url", url.strip()))
    head.append("HTTP %s · %d 字节%s" % (
        result.get("status"), result.get("bytes", 0),
        " · 已截断" if result.get("truncated") else ""))
    return ToolRet(True, "\n".join(head) + "\n\n" + (result.get("text") or ""),
                   truncated=bool(result.get("truncated")))


def run_web_search(args, ctx):
    import web as web_mod
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return ToolRet(False, "missing 'query' (搜索词)")
    result = web_mod.search(query, args.get("limit") or 8)
    if not result.get("ok"):
        return ToolRet(False, "web_search 失败: %s" % result.get("error", "未知错误"))
    lines = []
    for i, r in enumerate(result["results"], 1):
        lines.append("%d. %s\n   %s" % (i, r["title"], r["url"]))
        if r.get("snippet"):
            lines.append("   %s" % r["snippet"])
    return ToolRet(True, "搜索 \"%s\" 的结果:\n\n%s" % (query.strip(), "\n".join(lines)))


WEB_FETCH = {
    "name": "web_fetch",
    "description": ("抓一个网页并转成纯文本(剥脚本样式, 正文截 12000 字符). 只支持 http/https "
                    "公网地址; 跨站重定向不自动跟, 会返回新地址回执, 确认可信后用新地址再调一次. "
                    "适合查文档/读报错页/拉公开数据."),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "网页地址(裸域名自动补 https://)"},
            "timeout": {"type": "number", "description": "超时秒数, 默认 20, 上限 60"},
        },
        "required": ["url"],
    },
    "risk": "read",
    "run": run_web_fetch,
}

WEB_SEARCH = {
    "name": "web_search",
    "description": ("搜网页(DuckDuckGo 后端, 免 key), 返回 标题/链接/摘要. 适合查库用法/搜报错/"
                    "找官网; 上游限流或改版会如实报错, 换词重试或改用 web_fetch 抓已知地址."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "limit": {"type": "integer", "description": "返回条数, 默认 8, 上限 10"},
        },
        "required": ["query"],
    },
    "risk": "read",
    "run": run_web_search,
}


# ------ Tool: bg(M26 后台任务: 长命令不堵轮) ------

def run_bg(args, ctx):
    """后台任务. action=run 与 bash 工具过同一道门(权限 exec + pre 钩子) ——
    后台不等于绕门; 其余 action 只读日志/列表, 免门."""
    import bgtask as bg
    action = str(args.get("action") or "").strip()
    if action == "run":
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return ToolRet(False, "bg run: 缺 command(要后台跑的命令)")
        cmd = cmd.strip()
        fake_bash = {"name": "bash", "risk": "exec"}
        if ctx.perms is not None:
            verdict, by = ctx.perms.decide(fake_bash, {"command": cmd})
            if ctx.bus is not None:
                ctx.bus.emit("perm_decision", name="bash",
                             decision=verdict, allowed_by=by)
            if verdict != "allow":
                return ToolRet(False, "权限拒绝(%s): 后台运行未被允许. 不要原样重试, "
                                      "向用户说明." % by)
        if getattr(ctx, "hooks", None):
            import hooks as hooks_mod
            ok_h, reason, metas = hooks_mod.run_pre(ctx.hooks, "bash",
                                                    {"command": cmd})
            for m in metas:
                if ctx.bus is not None:
                    ctx.bus.emit("hook_run", phase="pre", **m)
            if not ok_h:
                return ToolRet(False, "钩子拒绝(bash): %s —— 不要原样重试." % reason)

        def on_done(tid, rc, seconds):
            if ctx.bus is not None:
                ctx.bus.emit("bg_done", id=tid, rc=rc, seconds=seconds,
                             cmd_head=cmd[:80])

        tid, path, _meta = bg.start(cmd, on_done=on_done)
        return ToolRet(True, "后台任务已起: %s(日志 %s)\n"
                      "收工会自动通知; 用 {\"action\":\"output\",\"id\":\"%s\"} 取输出, "
                      "{\"action\":\"stop\",\"id\":\"%s\"} 停止."
                      % (tid, path.replace("\\", "/"), tid, tid))
    if action == "output":
        tid = str(args.get("id") or "").strip()
        if not tid:
            return ToolRet(False, "bg output: 缺 id")
        if not bg.info(tid):
            return ToolRet(False, "没有后台任务 %s —— bg action=list 看清单" % tid)
        text = bg.output(tid)
        return ToolRet(True, "[%s 输出尾部] %s\n%s"
                       % (tid, "(还在跑)" if bg.info(tid)["running"] else "(已结束)",
                          text if text.strip() else "(暂无输出)"))
    if action == "stop":
        tid = str(args.get("id") or "").strip()
        if not tid:
            return ToolRet(False, "bg stop: 缺 id")
        return ToolRet(True, "已停 %s" % tid if bg.stop(tid)
                       else "%s 不存在或已结束, 无需停" % tid)
    if action == "list":
        rows = bg.list_tasks()
        if not rows:
            return ToolRet(True, "当前没有后台任务")
        return ToolRet(True, "\n".join(
            "%s %s rc=%s %ss %s" % (r["id"], "跑着" if r["running"] else "结束",
                                    r["rc"], r.get("seconds") or "-", r["cmd"][:60])
            for r in rows))
    return ToolRet(False, "bg: 未知 action %r —— run / output / stop / list" % action)


BG = {
    "name": "bg",
    "description": ("后台跑长命令(训练/批处理/长实验), 不占住当前轮: run 立即返回任务号, "
                    "输出落日志; 任务收工你会收到 bg_done 通知, 之后用 output 取尾部输出, "
                    "stop 停止, list 看清单. run 与 bash 同一道权限门(不因后台而绕门)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "run / output / stop / list"},
            "command": {"type": "string", "description": "action=run: 要后台跑的命令"},
            "id": {"type": "string", "description": "output/stop: 任务号(bgN)"},
        },
        "required": ["action"],
    },
    "risk": "read",   # 查询类免门; run 在工具内部过 bash 同款 exec 门+钩子
    "run": run_bg,
}


# ------ Tool: schedule(M27 定时任务) ------

def run_schedule(args, ctx):
    """定时任务. create/delete 在工具内部过 write 门(同 bg 的内部门套路);
    到点由前端心跳把 prompt 发进默认聊天窗 —— 那一轮的工具照常过门, 定时不绕门."""
    import sched as sched_mod
    action = str(args.get("action") or "").strip()
    if action == "create":
        fake = {"name": "schedule", "risk": "write"}
        if ctx.perms is not None:
            verdict, by = ctx.perms.decide(
                fake, {"action": "create", "cron": args.get("cron"),
                       "prompt": args.get("prompt")})
            if ctx.bus is not None:
                ctx.bus.emit("perm_decision", name="schedule",
                             decision=verdict, allowed_by=by)
            if verdict != "allow":
                return ToolRet(False, "权限拒绝(%s): 建定时任务未获允许." % by)
        entry, err = sched_mod.create(args.get("cron"), args.get("prompt"),
                                      args.get("label") or "",
                                      bool(args.get("recurring", True)))
        if entry is None:
            return ToolRet(False, "schedule create 失败: %s" % err)
        nf = sched_mod.next_fire(entry["cron"])
        return ToolRet(True, "已建定时任务 %s「%s」cron=%s%s\n只在 agent-term 面板开着时"
                       "触发; 到点把这句发给默认聊天窗: %s"
                       % (entry["id"], entry["label"], entry["cron"],
                          (" 下次 %s" % nf if nf else " (366 天内无命中!)"),
                          entry["prompt"][:80]))
    if action == "list":
        rows = sched_mod.list_entries()
        if not rows:
            return ToolRet(True, "当前没有定时任务")
        return ToolRet(True, "\n".join(
            "%s %s cron=%s%s 「%s」%s"
            % (e["id"], "循环" if e.get("recurring", True) else "一次", e["cron"],
               (" 下次 %s" % (sched_mod.next_fire(e["cron"]) or "?")),
               e.get("label") or "", e["prompt"][:50]) for e in rows))
    if action == "delete":
        tid = str(args.get("id") or "").strip()
        if not tid:
            return ToolRet(False, "schedule delete: 缺 id")
        if sched_mod.delete(tid):
            return ToolRet(True, "已删定时任务 %s" % tid)
        return ToolRet(False, "没有 %s 这条定时任务" % tid)
    return ToolRet(False, "schedule: 未知 action %r —— create / list / delete" % action)


SCHEDULE = {
    "name": "schedule",
    "description": ("定时任务(标准 5 段 cron: 分 时 日 月 周, 支持 * / a-b / 逗号 / */n): "
                    "到点把一段 prompt 发进默认聊天窗执行(提醒/例行扫描/每日汇总). 只在"
                    "面板开着时触发; recurring=false 为一次性. 查询/增删用 action="
                    "list/create/delete."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "create / list / delete"},
            "cron": {"type": "string", "description": "create: 5 段 cron, 如 '30 9 * * *'"},
            "prompt": {"type": "string", "description": "create: 到点发给 agent 的话"},
            "label": {"type": "string", "description": "create: 短标签(可空, 默认取 prompt 头)"},
            "recurring": {"type": "boolean", "description": "create: 默认 true; false=发一次即删"},
            "id": {"type": "string", "description": "delete: 条目号(tN)"},
        },
        "required": ["action"],
    },
    "risk": "read",   # 查询免门; create/delete 在工具内部过 write 门
    "run": run_schedule,
}


# ------ Tool: notebook(M30 ipynb 单元格编辑) ------

def _nb_load(path):
    """读 ipynb → (nb, err). 结构校验: 有 nbformat 与 cells 列表."""
    try:
        with open(path, encoding="utf-8") as f:
            nb = json.load(f)
    except FileNotFoundError:
        return None, "文件不存在: %s" % path
    except ValueError as exc:
        return None, "不是合法 JSON(%s)" % exc
    if not isinstance(nb, dict) or "nbformat" not in nb \
            or not isinstance(nb.get("cells"), list) or not nb["cells"]:
        return None, "不像 Jupyter notebook(缺 nbformat/cells)"
    return nb, ""


def _nb_find(nb, cell_id):
    """定位单元格: 先按 id 字段精确匹配, 再按 1 基序号. 返回 (下标, err)."""
    cells = nb["cells"]
    raw = str(cell_id).strip()
    for i, c in enumerate(cells):
        if str(c.get("id") or "") == raw and raw:
            return i, ""
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(cells):
            return n - 1, ""
    return -1, "找不到单元格 %r —— 用 read 工具看该 ipynb 的 cells(id 或 1 基序号)" % cell_id


def _nb_save(path, nb):
    """覆写前留 .bak(write 工具同款家法); indent=1 是 Jupyter 惯例."""
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(nb, f, ensure_ascii=False, indent=1)
            f.write("\n")
    except OSError as exc:
        return "写不进去(%s)" % exc
    return ""


def run_notebook(args, ctx):
    """ipynb 单元格编辑(对齐 Claude Code 的 NotebookEdit): edit/insert/delete.
    看内容用 read 工具(它就是 JSON 文本); cell_id 认 id 字段或 1 基序号."""
    path = args.get("notebook_path") or args.get("path")
    if not isinstance(path, str) or not path.strip():
        return ToolRet(False, "notebook: 缺 notebook_path")
    mode = str(args.get("edit_mode") or "edit").strip()
    nb, err = _nb_load(path.strip())
    if nb is None:
        return ToolRet(False, "notebook: %s" % err)
    cells = nb["cells"]
    cell_id = args.get("cell_id")

    def cell_desc(c):
        return "#%d %s %s" % (cells.index(c) + 1, c.get("cell_type", "?"),
                              str(c.get("id") or "(无id)"))

    if mode == "edit":
        src = args.get("new_source")
        if not isinstance(src, str):
            return ToolRet(False, "notebook: edit 缺 new_source")
        i, err = _nb_find(nb, cell_id)
        if i < 0:
            return ToolRet(False, "notebook: %s" % err)
        before = cell_desc(cells[i])
        cells[i]["source"] = src.splitlines(keepends=True)
        if cells[i].get("cell_type") == "code":
            cells[i]["outputs"] = []              # 旧输出对应旧代码, 留着只会骗人
            cells[i]["execution_count"] = None
        err = _nb_save(path.strip(), nb)
        if err:
            return ToolRet(False, "notebook: %s" % err)
        return ToolRet(True, "已改 %s(共 %d 格, 旧版留 .bak); code 格已清输出"
                       % (before, len(cells)))
    if mode == "insert":
        src = args.get("new_source")
        if not isinstance(src, str):
            return ToolRet(False, "notebook: insert 缺 new_source")
        ctype = str(args.get("cell_type") or "code")
        if ctype not in ("code", "markdown", "raw"):
            return ToolRet(False, "notebook: cell_type 只认 code/markdown/raw")
        if cell_id in (None, ""):
            at = 0                               # 不给定位 = 插最前(CC 同款)
            pos = "最前"
        else:
            i, err = _nb_find(nb, cell_id)
            if i < 0:
                return ToolRet(False, "notebook: %s" % err)
            at = i + 1                           # 插在定位格之后
            pos = "第 %d 格之后" % (i + 1)
        cell = {"cell_type": ctype, "metadata": {},
                "source": src.splitlines(keepends=True)}
        if ctype == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        # id 字段只有 nbformat≥4.5 才有; 有就补一个(用时间戳后缀防撞)
        if nb.get("nbformat", 4) >= 4 and nb.get("nbformat_minor", 5) >= 5:
            cell["id"] = "at%d" % int(time.time() * 1000 % 100000000)
        cells.insert(at, cell)
        err = _nb_save(path.strip(), nb)
        if err:
            return ToolRet(False, "notebook: %s" % err)
        return ToolRet(True, "已在%s插入 %s 格(新 #%d, 共 %d 格, 旧版留 .bak)"
                       % (pos, ctype, at + 1, len(cells)))
    if mode == "delete":
        i, err = _nb_find(nb, cell_id)
        if i < 0:
            return ToolRet(False, "notebook: %s" % err)
        gone = cell_desc(cells[i])
        cells.pop(i)
        err = _nb_save(path.strip(), nb)
        if err:
            return ToolRet(False, "notebook: %s" % err)
        return ToolRet(True, "已删 %s(剩 %d 格, 旧版留 .bak)" % (gone, len(cells)))
    return ToolRet(False, "notebook: edit_mode 只认 edit/insert/delete, 收到 %r" % mode)


NOTEBOOK = {
    "name": "notebook",
    "description": ("编辑 Jupyter notebook(.ipynb) 的单元格: edit=改源码(code 格顺手清旧"
                    "输出), insert=插入新格(不给 cell_id=插最前), delete=删格. cell_id 认"
                    "单元格 id 字段或 1 基序号; 看内容先用 read 工具. 每次覆写自动留 .bak."),
    "input_schema": {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string", "description": "ipynb 路径"},
            "cell_id": {"type": "string",
                        "description": "单元格 id 或 1 基序号(insert 不给=插最前)"},
            "new_source": {"type": "string", "description": "新源码(edit/insert 必填)"},
            "edit_mode": {"type": "string", "description": "edit / insert / delete"},
            "cell_type": {"type": "string",
                          "description": "insert 的新格类型: code(默认)/markdown/raw"},
        },
        "required": ["notebook_path"],
    },
    "risk": "write",   # 改文件, 与 write/edit 同门
    "run": run_notebook,
}


BUILTIN = [BASH, READ, WRITE, EDIT, GREP, GLOB, TODO, MEMORY, MEMORY_WRITE, ASK_USER,
           PLATFORM, SPAWN, LOOK_IMAGE, IMAGE_GEN, SKILL, LEDGER, LEDGER_SCORE,
           WEB_FETCH, WEB_SEARCH, PLAN_SUBMIT, BG, SCHEDULE, NOTEBOOK]


def build_registry(tools=None, exclude=()):
    reg = ToolRegistry()
    for t in (tools if tools is not None else BUILTIN):
        if t["name"] not in exclude:
            reg.register(t)
    return reg

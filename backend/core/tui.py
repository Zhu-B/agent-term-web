# -*- coding: utf-8 -*-
"""tui.py — 终端 UI: 输入行编辑器(LineEditor) + 彩色渲染 sink(render_sink).

布局策略: 不用全屏 alt-screen —— 输出区=普通终端滚动(历史天然在 scrollback),
输入行=屏幕最后一行就地重绘, 与 Claude Code 同形态.

隔离缝: InputBackend 封死全部平台输入细节(Windows=msvcrt, POSIX=termios),
将来要换 prompt_toolkit(本机已装 3.0.50)只动这一层, LineEditor 不动.

Windows 三坑的对付:
  1. conhost 默认不认 ANSI → 启动时 ctypes 开 ENABLE_VIRTUAL_TERMINAL_PROCESSING;
  2. QuickEdit 鼠标选区会阻塞 stdout 写(TUI 假死第一坑) → 关 ENABLE_QUICK_EDIT_MODE;
  3. Ctrl+C 在 msvcrt.getwch 处可能异步抛 KeyboardInterrupt → backend 统一接住,
     折叠成 ("ctrl_c","") 键事件上报.
编辑全部在 Python str(码点)层做, 绝不碰字节 —— 中文/emoji 退格安全的基础;
显示宽度用 east_asian_width 估列, 光标定位按显示宽算.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unicodedata

try:
    import msvcrt
except ImportError:  # POSIX(WSL)侧没有 msvcrt
    msvcrt = None

# ------ ANSI ------
DIM, BOLD = "\x1b[2m", "\x1b[1m"
CYAN, GREEN, RED, YELLOW = "\x1b[36m", "\x1b[32m", "\x1b[31m", "\x1b[33m"
RST = "\x1b[0m"


# ------ Key decode(纯函数, 可无 tty 单测) ------
def plain_key(c):
    """普通字符键 → (kind, ch). CR/DEL/ESC/Ctrl+C 等 Windows/POSIX 共用一张表."""
    if c in ("\r", "\n"):
        return ("enter", "")
    if c == "\x03":
        return ("ctrl_c", "")
    if c in ("\b", "\x7f"):
        return ("backspace", "")
    if c == "\t":
        return ("tab", "")
    if c == "\x1b":
        return ("esc", "")
    return ("char", c)


def vk_second(c):
    """msvcrt 特殊键第二字节 → 方向/编辑键 kind."""
    return {"H": "up", "P": "down", "K": "left", "M": "right",
            "G": "home", "O": "end", "S": "delete"}.get(c, "other")


def ansi_second(c):
    """ANSI 转义序列末字节(CSI X) → kind."""
    return {"A": "up", "B": "down", "C": "right", "D": "left",
            "H": "home", "F": "end"}.get(c, "other")


def display_width(s) -> int:
    """显示宽估算: 全角(W/F)记 2 列, 其余 1 列. 光标列计算的基础."""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


# ------ InputBackend 隔离缝 ------
class InputBackend:
    def read_key(self):
        raise NotImplementedError

    def close(self):
        pass


class WindowsBackend(InputBackend):
    def read_key(self):
        try:
            c = msvcrt.getwch()
        except KeyboardInterrupt:  # 坑 3: 异步 SIGINT 折叠成键事件
            return ("ctrl_c", "")
        if c in ("\x00", "\xe0"):  # 特殊键前缀, 真键在第二字节
            try:
                return (vk_second(msvcrt.getwch()), "")
            except KeyboardInterrupt:
                return ("ctrl_c", "")
        return plain_key(c)


class PosixBackend(InputBackend):
    """termios raw 模式. raw 下 Ctrl+C 不再产生信号, \x03 由我们自己判."""

    def __init__(self):
        import termios
        import tty
        self._termios, self._tty = termios, tty
        self.fd = sys.stdin.fileno()
        self.orig = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)

    def read_key(self):
        c = sys.stdin.read(1)
        if c == "\x1b":
            import select
            if select.select([sys.stdin], [], [], 0.05)[0]:
                if sys.stdin.read(1) == "[":
                    return (ansi_second(sys.stdin.read(1)), "")
            return ("esc", "")
        return plain_key(c)

    def close(self):
        try:
            self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self.orig)
        except Exception:
            pass


class ScriptedBackend(InputBackend):
    """测试缝: 预置键事件序列, 供无 tty 环境驱动 LineEditor/REPL."""

    def __init__(self, keys):
        # 裸字符串 = 一整行(list(str) 会把它拆成单字符行, 那是误用); 列表 = 键事件序
        self.keys = [keys] if isinstance(keys, str) else list(keys)
        self.i = 0

    def read_key(self):
        if self.i >= len(self.keys):
            return ("ctrl_c", "")
        k = self.keys[self.i]
        if isinstance(k, str):  # 语法糖: 单字符串 → 逐字符 + 收尾 enter
            seq = [("char", ch) for ch in k] + [("enter", "")]
            self.keys[self.i:self.i + 1] = seq
            k = self.keys[self.i]
            self.i += 1
            return k
        self.i += 1
        return k


def make_backend():
    if os.name == "nt" and msvcrt is not None:
        return WindowsBackend()
    return PosixBackend()


# ------ LineEditor ------
class LineEditor:
    """就地重绘的单行编辑器: 普通编辑 + 历史(上/下) + Ctrl+C(返 None 由调用方定语义)."""

    def __init__(self, prompt="at> ", backend=None, history=None, writer=None):
        self.prompt = prompt
        self.backend = backend or make_backend()
        self.history = [h for h in (history or []) if h.strip()]
        self.w = writer if writer is not None else sys.stdout

    def readline(self):
        buf, pos, draft, hist_idx = [], 0, "", None
        self._render(buf, pos)
        while True:
            kind, ch = self.backend.read_key()
            if kind == "enter":
                self.w.write("\r\n")
                self.w.flush()
                text = "".join(buf)
                if text.strip() and (not self.history or self.history[-1] != text):
                    self.history.append(text)
                return text
            if kind == "ctrl_c":
                self.w.write("\r\n")
                self.w.flush()
                return None
            if kind == "char":
                buf.insert(pos, ch)
                pos += 1
            elif kind == "backspace":
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif kind == "delete":
                if pos < len(buf):
                    del buf[pos]
            elif kind == "left":
                pos = max(0, pos - 1)
            elif kind == "right":
                pos = min(len(buf), pos + 1)
            elif kind == "home":
                pos = 0
            elif kind == "end":
                pos = len(buf)
            elif kind == "esc":
                buf, pos = [], 0
            elif kind == "up":
                if self.history:
                    if hist_idx is None:
                        draft, hist_idx = "".join(buf), len(self.history) - 1
                    else:
                        hist_idx = max(0, hist_idx - 1)
                    buf, pos = list(self.history[hist_idx]), len(self.history[hist_idx])
            elif kind == "down":
                if hist_idx is not None:
                    if hist_idx >= len(self.history) - 1:
                        hist_idx = None
                        buf, pos = list(draft), len(draft)
                    else:
                        hist_idx += 1
                        buf, pos = list(self.history[hist_idx]), len(self.history[hist_idx])
            # tab/other: 忽略(补全是后续里程碑的活)
            self._render(buf, pos)

    def _render(self, buf, pos):
        text = "".join(buf)
        self.w.write("\r\x1b[K" + self.prompt + text)
        col = display_width(self.prompt) + display_width(text[:pos]) + 1
        self.w.write("\x1b[%dG" % col)
        self.w.flush()


# ------ Permission confirm(M2) ------
def confirm(tool, args) -> str:
    """权限确认框. 返回 "allow" | "always" | "deny". 回车/无效输入=拒绝(安全默认);
    Ctrl+C 不在这里接 —— 让它冒泡给 run_turn 当"拒绝并中断整轮"处理."""
    name = tool.get("name", "?")
    if name == "bash":
        detail = str(args.get("command") or "")[:80]
    elif name in ("write", "edit"):
        detail = str(args.get("path") or args.get("file_path") or "")
    else:
        detail = json.dumps(args, ensure_ascii=False)[:80] if args else ""
    print(YELLOW + BOLD + "⚠ %s %s" % (name, detail) + RST, flush=True)
    while True:
        try:
            ans = input(YELLOW + "[y]允许 [a]本会话总允许 [n]拒绝(回车): " + RST).strip().lower()
        except EOFError:
            return "deny"
        if ans in ("y", "yes"):
            return "allow"
        if ans == "a":
            return "always"
        if ans in ("", "n", "no"):
            return "deny"


# ------ Console prep(Windows) ------
def enable_vt():
    """开 VT 处理 + 关 QuickEdit, 返回 restore() 或 None(非 Windows/管道/失败)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        saved = {}

        def set_mode(hstd, mask_set, mask_clear):
            if not hstd or hstd == -1 or hstd == 0xFFFFFFFFFFFFFFFF:
                return
            if not k32.GetConsoleMode(hstd, ctypes.byref(mode)):
                return  # 管道/重定向: 拿不到就是没有, 不折腾
            saved[hstd] = mode.value
            k32.SetConsoleMode(hstd, (mode.value | mask_set) & ~mask_clear)

        h_in, h_out = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
        set_mode(h_in, 0x80, 0x40)   # EXTENDED_FLAGS 置位, QuickEdit 清位(坑 2)
        set_mode(h_out, 0x4, 0x0)    # ENABLE_VIRTUAL_TERMINAL_PROCESSING(坑 1)

        def restore():
            try:
                for hstd, m in saved.items():
                    k32.SetConsoleMode(hstd, m)
            except Exception:
                pass

        return restore
    except Exception:
        return None


# ------ Render sink ------
def render_sink():
    """彩色版事件渲染(M1 的 UI, 替换 M0 的 plain_sink; --plain 仍用旧版降级).
    M6: llm_chunk 增量直写(正文原色逐字, 思考流暗色), llm_response 只补落款."""
    st = {"streamed": False}

    def sink(ev):
        t, d = ev["type"], ev["data"]
        if d.get("internal"):             # 内部机械调用(记忆整理等)不渲染
            return
        if d.get("sub"):
            return    # M14: 分身事件不进终端(并行交织会毁掉滚动区); 结论在 spawn 的
                      # tool_result 摘要里, 信息不丢
        if t == "turn_start":
            st["streamed"] = False
            print("\n" + CYAN + BOLD + "> " + RST + CYAN + d.get("user_text", "") + RST,
                  flush=True)
        elif t == "llm_request":
            n = len((d.get("payload") or {}).get("messages") or [])
            print(DIM + "· %s 请求中 (messages=%d)… Ctrl+C 可中断"
                  % (d.get("model"), n) + RST, flush=True)
        elif t == "llm_chunk":
            txt = d.get("text") or ""
            if txt:
                if d.get("kind") == "reasoning":
                    sys.stdout.write(DIM + txt + RST)
                else:
                    sys.stdout.write(txt)
                sys.stdout.flush()
                st["streamed"] = True
        elif t == "llm_response":
            u = d.get("usage") or {}
            tag = " (cached)" if d.get("cached") else ""
            footer = DIM + "◷ %s %.1fs · in=%s out=%s%s" % (
                d.get("model"), d.get("latency_ms", 0) / 1000.0,
                u.get("prompt_tokens", "?"), u.get("completion_tokens", "?"), tag) + RST
            if st["streamed"]:             # 正文已逐字流过: 收行+落款, 不重打
                st["streamed"] = False
                print(flush=True)
                print(footer, flush=True)
                return
            print(footer, flush=True)
            msg = d.get("message") or {}
            text = msg.get("content") or ""
            inter = bool(msg.get("tool_calls"))  # 带工具调用的中间叙事 → 暗色缩进
            for line in text.splitlines():
                print((DIM + "  " + line + RST) if inter else line, flush=True)
        elif t == "tool_call":
            args = str(d.get("args_raw", "")).replace("\n", " ")
            print(CYAN + "● %s %s" % (d.get("name"), args[:120]) + RST, flush=True)
        elif t == "tool_result":
            ok = d.get("ok")
            color = GREEN if ok else RED
            size = d.get("content_len", 0)
            trunc = " · 截断" if d.get("truncated") else ""
            print(color + "  ⎿ %s %.1fs · %dB%s"
                  % ("ok" if ok else "err", d.get("duration_ms", 0) / 1000.0,
                     size, trunc) + RST, flush=True)
            lines = (d.get("_content") or "").splitlines() or ["(空)"]
            for line in lines[:8]:
                print((color if not ok else "") + "  " + line[:100] + (RST if not ok else ""),
                      flush=True)
            if len(lines) > 8:
                print(DIM + "  …(还有 %d 行)" % (len(lines) - 8) + RST, flush=True)
        elif t == "perm_decision":
            print(YELLOW + "⚠ 权限: %s → %s" % (d.get("name"), d.get("decision"))
                  + RST, flush=True)
        elif t == "todo_update":
            # 任务清单: 与工具回喂共用 render_todos, 配色区分三态
            mark_color = {"pending": DIM, "in_progress": YELLOW, "done": GREEN}
            print(YELLOW + "☑ 任务清单" + RST, flush=True)
            for i, it in enumerate(d.get("items") or []):
                c = mark_color.get(it.get("status"), DIM)
                mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(
                    it.get("status"), "[?]")
                print(c + "  %d. %s %s" % (i + 1, mark, it.get("content", "")) + RST,
                      flush=True)
        elif t == "error":
            print(RED + "✗ %s: %s (attempt %s)"
                  % (d.get("where"), d.get("repr_"), d.get("attempt")) + RST, flush=True)

    return sink


# ------ History file ------
def load_history(path, cap=500):
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()][-cap:]
    except OSError:
        return []


def save_history(path, items, cap=500):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(items[-cap:]) + "\n")
    except OSError:
        pass


class ScriptedEditor:
    """测试缝: readline() 依次吐预置行, 用尽返 /exit(相当于干净退出).

    注意不返 None: REPL 把 None 当 Ctrl+C(双击退出语义), 测试缝直接走 /exit."""

    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else "/exit"

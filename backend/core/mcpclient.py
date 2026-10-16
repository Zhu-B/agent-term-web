# -*- coding: utf-8 -*-
"""mcpclient.py — MCP client(M4): spawn stdio server, 新行分隔 JSON-RPC, 全生命周期.

对端方言(与 wechat-eye/server.py 实测一致, 也是 MCP stdio 的通行做法):
每行一个 JSON-RPC 消息; initialize → notifications/initialized → tools/list →
tools/call; 无 id 的是 notification 不用等回. client 侧: 自增 id + {id: Queue}
分发, daemon reader 线程逐行读对端 stdout.

子进程纪律(计划风险清单 #6, 两条都有血泪出处):
  - stdin=PIPE 是我们主动喂的协议通道, 与 bash 工具的 DEVNULL 铁律不矛盾
    (铁律防意外继承, 这里是故意喂);
  - stderr 必须重定向到文件 —— 子进程 stderr 写满 pipe 缓冲区 = 整条通道死锁;
  - 退出 atexit 兜底, Windows 上 taskkill /T /F 树杀防孤儿 python.

清单(mcp.json)支持 "__BASE__" 占位符 = 本平台目录, 便于相对路径跨机器可移植.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time

import instr
import tools

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "run")


class McpClient:
    def __init__(self, name, cmd, cwd=None, env=None, timeout=60.0,
                 defaults=None, force=None, risk="read", hint=None, platform=None):
        self.name = name
        self.cmd = [str(c).replace("__BASE__", BASE) for c in cmd]
        self.cwd = (cwd or "").replace("__BASE__", BASE) or None
        self.env = env or None
        self.timeout = float(timeout)
        self.defaults = defaults or {}    # 基础参数层(模型可覆盖)
        self.force = force or {}          # 强制参数层(永远赢, 如 attach_image=false)
        self.risk = risk                  # 该 server 全部工具的权限风险级
        self.hint = hint or ""            # 追加到每个工具描述的使用提示(跟清单走)
        self.platform = platform or ""    # 平台声明(windows/macos/linux), 不符则不挂载
        self.proc = None
        self.alive = False
        self.last_error = ""
        self._queues = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._atexit_done = False

    # ------ Lifecycle ------
    def start(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, "mcp-%s.log" % self.name)
        # stderr→文件: 防子进程 stderr 写满管道缓冲把通道锁死
        log_f = open(log_path, "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=log_f,
                                     cwd=self.cwd, env=self.env,
                                     creationflags=flags)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.alive = True
        atexit.register(self.shutdown)
        return self

    def shutdown(self):
        if not self.alive or self.proc is None:
            self.alive = False
            return
        self.alive = False
        try:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    # stdin=DEVNULL 纪律同样适用于 taskkill 自己
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, timeout=10,
                                   stdin=subprocess.DEVNULL)
                else:
                    self.proc.terminate()
        except Exception:
            pass
        finally:
            with self._lock:
                for q in self._queues.values():
                    q.put(None)   # 唤醒所有还在等响应的调用方
                self._queues.clear()

    # ------ Protocol ------
    def _read_loop(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()   # 对端可能写 \r\n
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                with self._lock:
                    q = self._queues.pop(mid, None)
                if q is not None:
                    q.put(msg)
        except Exception:
            pass
        finally:
            with self._lock:
                for q in self._queues.values():
                    q.put(None)
                self._queues.clear()

    def _rpc(self, method, params=None, notify=False, timeout=None):
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            q = queue.Queue()
            if not notify:
                self._queues[mid] = q
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = mid
        try:
            # ensure_ascii=True: 非 ASCII 全转 \uXXXX 转义 —— 纯 ASCII 字节在任何
            # 编码假定的对端都解出同一结果(中文 Windows 起的 python server 若没
            # reconfigure stdin, 默认按 GBK+surrogateescape 读 UTF-8 字节 → 乱码
            # 甚至 UnicodeEncodeError 崩掉整条通道, _verify spawn:MCP 实锤过).
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception as ex:
            self.last_error = "write failed: %s" % ex
            if not notify:
                with self._lock:
                    self._queues.pop(mid, None)
            return None
        if notify:
            return None
        try:
            msg = q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            with self._lock:
                self._queues.pop(mid, None)   # 迟到响应来了也没人接, 丢弃
            self.last_error = "timeout after %ss" % (timeout or self.timeout)
            return None
        if msg is None:
            self.last_error = "server exited"
            return None
        if "error" in msg:
            self.last_error = "rpc error: %s" % msg["error"]
            return None
        return msg.get("result")

    def handshake(self):
        r = self._rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "agent-term", "version": "0.1.0"}}, timeout=15)
        if r is None:
            raise RuntimeError("initialize 失败: %s" % self.last_error)
        self._rpc("notifications/initialized", notify=True)
        return r

    # ------ Tools ------
    def list_tools(self):
        r = self._rpc("tools/list") or {}
        return [self._to_tooldef(t) for t in (r.get("tools") or [])]

    def _to_tooldef(self, t):
        raw_name = t.get("name", "")
        desc = ("[mcp:%s] %s" % (self.name, t.get("description", ""))).strip()
        if self.hint:
            desc += " 使用提示: %s" % self.hint
        return {
            "name": "mcp__%s__%s" % (self.name, raw_name),
            "description": desc,
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            "risk": self.risk,
            "run": lambda args, ctx, _n=raw_name: self._run_tool(_n, args),
            "source": "mcp:%s" % self.name,
        }

    def _run_tool(self, tool, args):
        merged = dict(self.defaults.get(tool) or {})
        merged.update(args or {})
        merged.update(self.force.get(tool) or {})   # 强制层永远赢
        result = self._rpc("tools/call", {"name": tool, "arguments": merged})
        if result is None:
            return tools.ToolRet(False, "MCP %s/%s 调用失败: %s"
                                 % (self.name, tool, self.last_error))
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        text = "\n".join(x for x in texts if x)
        if result.get("isError"):
            return tools.ToolRet(False, text or "MCP error (无文本)")
        # M15: 图像不再丢 —— 本体落媒体池(红线 4), 引用随 ToolRet 上走;
        # 附不附图(喂不喂模型)由回填缝按当前模型是否视觉档决定
        imgs = []
        for p in parts:
            if p.get("type") != "image":
                continue
            ref = instr.save_media(p.get("data") or "", p.get("mimeType") or "image/png")
            if ref:
                imgs.append(ref)
            else:
                text += "\n(1 张附图解码失败, 已丢弃)"
        if imgs:
            text += "\n(附图 %d 张)" % len(imgs)
        return tools.ToolRet(True, text or "(空响应)", images=imgs or None)


# ------ Manifest ------
def _cur_platform() -> str:
    """本机平台的人话名, 与清单 platform 字段同词表(windows/macos/linux/...)."""
    return {"win32": "windows", "darwin": "macos", "cygwin": "windows"}.get(
        sys.platform, sys.platform)


def _manifest_path(path=None):
    """选清单: 显式 path > AT_MCP env > mcp.json > mcp.example.json.
    最后一级兜底 = 开箱即用: 同事 clone 完不用配任何东西, 示例默认(如 wechat-eye)直接挂."""
    for cand in (path, os.environ.get("AT_MCP"),
                 os.path.join(BASE, "mcp.json"), os.path.join(BASE, "mcp.example.json")):
        if cand and os.path.exists(cand):
            return cand
    return None


def load_mcp(path=None):
    """读清单 → [McpClient]. 没有清单/空清单 = 零 MCP 照跑."""
    path = _manifest_path(path)
    if path is None:
        return []
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    servers = cfg.get("servers") if isinstance(cfg, dict) else cfg
    out = []
    for s in servers or []:
        if not s.get("cmd") or not s.get("name"):
            continue
        out.append(McpClient(s["name"], s["cmd"], cwd=s.get("cwd"), env=s.get("env"),
                             timeout=s.get("timeout", 60),
                             defaults=s.get("defaults"), force=s.get("force"),
                             risk=s.get("risk", "read"), hint=s.get("hint"),
                             platform=s.get("platform")))
    return out


def boot_mcp(registry, path=None):
    """启动清单里全部 server, 握手, 工具并进注册表. 返回启动成功的 client 列表
    (调用方退出时逐个 shutdown). 单个失败不拖垮平台, 打印一行继续."""
    mp = _manifest_path(path)
    if mp is None:
        return []
    if os.path.basename(mp) == "mcp.example.json":
        print("[mcp] 无本地 mcp.json, 按内置示例默认挂载(要定制就复制 mcp.example.json 为 mcp.json)",
              flush=True)
    started = []
    for c in load_mcp(mp):
        if c.platform and c.platform != _cur_platform():
            # 平台不符(如 wechat-eye 的 Win32/WinRT 依赖)在挂载前就拦下:
            # 模型看不见这些工具, 也就不会调出满屏 powershell 报错
            print("[mcp] %s 声明仅支持 %s, 本机是 %s —— 跳过(要强行挂就删清单里的 platform 字段)"
                  % (c.name, c.platform, _cur_platform()), flush=True)
            continue
        missing = next((a for a in c.cmd[1:]
                        if a.startswith(BASE) and not os.path.exists(a)), None)
        if missing:
            # 例行清单指向 __BASE__/wechat-eye 而 clone 时没带 submodule —— 与其让
            # python 空转 15s 握手超时, 不如直接告诉他差哪一步
            print("[mcp] %s 启动失败(跳过): 目标 %s 不存在 —— 先 git submodule update --init, "
                  "或按 README 手动 clone" % (c.name, missing), flush=True)
            continue
        try:
            c.start()
            c.handshake()
            tds = c.list_tools()
            for td in tds:
                registry.register(td)
            started.append(c)
            print("[mcp] %s 已挂载 %d 个工具: %s"
                  % (c.name, len(tds), ", ".join(sorted(t["name"] for t in tds))),
                  flush=True)
        except Exception as ex:
            print("[mcp] %s 启动失败(跳过): %s: %s" % (c.name, type(ex).__name__, ex),
                  flush=True)
            c.shutdown()
    return started

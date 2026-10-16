# -*- coding: utf-8 -*-
"""driver.py — M7 无头驱动接口(AgentApp): REPL / 未来前端 / 嵌入共用的一层.

at.py 的终端 REPL 是第一个壳(只管呈现); 软件风格前端是第二个壳:
    from driver import AgentApp
    app = AgentApp(provider, cwd=..., perms=...)
    app.subscribe(gui_sink)          # 事件总线再挂一个渲染/采集 sink
    summary = app.send("任务...")    # 同步一轮; 全部过程事件走总线
    app.knob_set("effort", "low") · app.compact() · app.resume(sid) · app.sessions()

边界: 本层不碰网络/线程/终端 —— 将来服务化(HTTP/WS)是在这层外面再包壳,
不在里面加; 事件总线是唯一的过程观测面(前端渲染 = 订阅事件).
"""
from __future__ import annotations

import json
import os

import agent
import instr
import prompts as prompts_mod
import session as session_mod
import tui
from tools import build_registry


def build_sysprompt(p, cwd, registry):
    """装配系统提示词, 顺带返回 AGENT.md 发现结果(banner 展示用)."""
    md = prompts_mod.discover_agent_md(cwd)
    sp = prompts_mod.build_sysprompt(cwd, registry=registry,
                                     provider=p.name, model=p.model, agent_md=md)
    return sp, md


class AgentApp:
    """一次装配 = 会话 + 总线 + agent 循环; /resume /clear = 原地重建."""

    def __init__(self, provider, cwd=None, plain=False, perms=None,
                 registry=None, knobs=None, resume=False, mode="app", quiet=False,
                 stream=False, auto_memory=False, sysprompt_extra=None,
                 ask_user=None, platform=None, memory_level="standard",
                 memory_overwrite=None, sub_provider_fn=None, sub_models=None,
                 sub_allow_other=None, image_gen=None, hooks=None,
                 sub_vision_models=None):
        self.provider = provider
        self.hooks = hooks   # M25: 工具前后钩子快照(前端/REPL 注入; None=不挂)
        self.cwd = cwd or os.getcwd()
        self.plain = plain
        self.quiet = quiet    # 前端模式: 不挂终端渲染 sink(UI 从事件总线订阅)
        self.perms = perms
        self.registry = registry or build_registry()
        self.knobs = knobs if knobs is not None else {}   # 同一 dict: /knob 原地改
        self.stream = stream  # M6: SSE 流式(前端默认开; runlab 恒关保数据可比)
        self.auto_memory = auto_memory  # M9.3: 轮末自动记忆(前端默认开)
        self.memory_level = memory_level  # 轮末记忆激进度(设置页; 新开窗生效)
        self.memory_overwrite = memory_overwrite  # 同名覆盖策略 ask/keep/auto(设置页)
        # M12b: 客制化约束块, 追加在系统提示词最末(近因最强); 默认 None 零影响
        self.sysprompt_extra = sysprompt_extra or ""
        self.ask_user = ask_user   # ask_user 工具的选择通道(前端注入); None=无界面
        self.platform = platform   # platform 工具的界面直控通道(前端注入); None=无界面
        # M14: spawn 指定模型的解析器 label("家/型号")->Provider(前端注入); None=不支持指定
        self.sub_provider_fn = sub_provider_fn
        self.sub_models = sub_models   # 可调用清单(list/callable)——spawn 报错回喂候选
        self.sub_allow_other = sub_allow_other   # 设置页「分身允许用非当前模型」(bool/callable)
        self.image_gen = image_gen   # M15: 画图档解析器 spec->{base_url,key,model}(前端注入)
        self.sub_vision_models = sub_vision_models   # M32: 视觉档候选清单(look_image 回喂用)
        self.mcp = []
        self._start(resume=resume, mode=mode)

    # ---- 装配(session + bus + loop; resume/clear 复用同一条路) ----
    def _start(self, resume=False, mode="app", path=None):
        preload = todos = None
        if path is not None:
            sess = session_mod.Session.resume(path)
            preload, todos = sess.messages(), sess.todos()
        elif resume and session_mod.Session.latest():
            sess = session_mod.Session.resume(session_mod.Session.latest())
            preload, todos = sess.messages(), sess.todos()
        else:
            sess = session_mod.Session.new(cwd=self.cwd)
        sess.meta(provider=self.provider.name, model=self.provider.model,
                  cwd=self.cwd, mode=mode)
        self.session = sess
        self.trace_path = os.path.join(instr.TRACE_DIR, sess.sid + ".jsonl")
        self.bus = instr.EventBus(sess.sid, self._base_sinks())
        sp = build_sysprompt(self.provider, self.cwd, self.registry)[0]
        if self.sysprompt_extra:               # M12b: 客制化的行为层防线(最末=近因最强)
            sp += "\n\n" + self.sysprompt_extra
        self.loop = agent.AgentLoop(self.provider, self.registry, self.bus,
                                    sysprompt=sp, cwd=self.cwd, session=sess,
                                    preload=preload, perms=self.perms,
                                    knobs=self.knobs, todos=todos, stream=self.stream,
                                    auto_memory=self.auto_memory,
                                    ask_user=self.ask_user, platform=self.platform,
                                    memory_level=self.memory_level,
                                    memory_overwrite=self.memory_overwrite,
                                    sub_provider_fn=self.sub_provider_fn,
                                    sub_models=self.sub_models,
                                    sub_allow_other=self.sub_allow_other,
                                    image_gen=self.image_gen,
                                    hooks=self.hooks,
                                    sub_vision_models=self.sub_vision_models)

    def _base_sinks(self):
        sinks = [instr.raw_store_sink(), instr.jsonl_sink(self.trace_path)]
        if not self.quiet:
            sinks.append(instr.plain_sink() if self.plain else tui.render_sink())
        return sinks

    # ---- 对外动作 ----
    def send(self, text):
        """跑一轮 agent(同步). 返回 TurnSummary; 过程事件全走总线."""
        return self.loop.run_turn(text)

    def subscribe(self, sink):
        """运行中挂新 sink(前端渲染器/实验采集器)."""
        self.bus.add_sink(sink)

    def attach_mcp(self):
        """boot mcp.json 声明的外挂工具(REPL 用; 测试/嵌入不自动挂)."""
        import mcpclient
        self.mcp = mcpclient.boot_mcp(self.registry)
        return self.mcp

    def shutdown(self):
        for c in self.mcp or []:
            try:
                c.shutdown()
            except Exception:
                pass
        try:
            self.session.close()
        except Exception:
            pass

    def compact(self):
        """上下文压缩(手动入口; 自动水位在 loop.run_turn 里)."""
        return self.loop.compact()

    def knob_set(self, key, value):
        self.loop.knobs[key] = value

    def knob_clear(self, key):
        self.loop.knobs.pop(key, None)

    def todos(self):
        return self.loop.ctx.todos

    def inject_memory(self, title, content):
        """把一张记忆卡注入当前会话(面板点卡走这). 消息进 messages+transcript,
        并发 memory_read 事件 —— UI 标已读与工具读走同一条路, 口径不漂移."""
        self.loop._append({"role": "system",
                           "content": "【记忆卡·%s】用户指定注入的跨会话记忆, "
                                      "作为本会话的既有事实使用:\n\n%s" % (title, content)})
        self.bus.emit("memory_read", title=title)

    def inject_note(self, text, kind="note"):
        """把一条系统注记注入当前会话(M24 计划模式开关等): 只进 messages+transcript,
        不发 memory_read(那会误标卡列已读). note_injected 事件供 UI 同步, 不渲染正文."""
        self.loop._append({"role": "system", "content": text})
        self.bus.emit("note_injected", kind=kind, length=len(text))

    def interrupt(self):
        """请求中断当前轮(线程安全标志; REPL 的 Ctrl+C 走 KeyboardInterrupt, 前端走这)."""
        self.loop.abort.set()

    def resume(self, prefix=None):
        """续会话: prefix=sid 前缀精确选(唯一命中), None=最近一个(非当前).
        返回 (session, ""); 失败 (None, 原因)."""
        files = self.session_files()
        if prefix:
            hits = [f for f in files if f[:-6].startswith(prefix)]
            if not hits:
                return None, "没有 sid 以 %r 开头的会话(/sessions 看清单)" % prefix
            if len(hits) > 1:
                return None, "前缀 %r 命中 %d 个, 再补长一点: %s" % (
                    prefix, len(hits), ", ".join(f[:-6] for f in hits))
            path = os.path.join(session_mod.SESSIONS_DIR, hits[0])
        else:
            cur = os.path.basename(self.session.path)
            cands = [f for f in files if f != cur] or files
            if not cands:
                return None, "没有别的可续会话"
            path = os.path.join(session_mod.SESSIONS_DIR, cands[-1])
        try:
            self.session.close()
        except Exception:
            pass
        self._start(mode="resume", path=path)
        self.session.meta(provider=self.provider.name, model=self.provider.model,
                          event="resume")
        return self.session, ""

    def clear(self):
        """开新会话(旧 transcript 原样留在 sessions 目录)."""
        try:
            self.session.close()
        except Exception:
            pass
        self._start(mode="clear")
        self.session.meta(provider=self.provider.name, model=self.provider.model,
                          event="clear")
        return self.session

    # ---- 会话清单(前端会话栏的数据源) ----
    def sessions(self, limit=20):
        """最近会话列表(按文件名=时间倒序): sid/provider/model/消息数/mtime/当前."""
        rows = []
        cur_sid = self.session.sid
        for fn in reversed(self.session_files()[:limit]):
            path = os.path.join(session_mod.SESSIONS_DIR, fn)
            sid = fn[:-6]
            n_msgs = 0
            provider = model = ""
            try:
                with open(path, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            d = json.loads(ln)
                        except Exception:
                            continue
                        if d.get("kind") == "message":
                            n_msgs += 1
                        elif d.get("kind") == "meta":
                            obj = d.get("obj") or {}
                            provider = obj.get("provider") or provider
                            model = obj.get("model") or model
            except OSError:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            rows.append({"sid": sid, "provider": provider, "model": model,
                         "messages": n_msgs, "mtime": mtime,
                         "current": sid == cur_sid})
        return rows

    @staticmethod
    def session_files():
        try:
            return sorted(f for f in os.listdir(session_mod.SESSIONS_DIR)
                          if f.endswith(".jsonl"))
        except OSError:
            return []

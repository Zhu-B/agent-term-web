# -*- coding: utf-8 -*-
"""agent.py — agent 主循环: LLM → tool_calls → 执行回填 → 再循环, 直到无 tool_calls.

对应 Claude Code 的 agent loop 子系统(学习复刻参照系之一).

循环不变量(红线):
  1. 每个 tool_call_id 必须有配对的 role=tool 消息 —— 中断/失败路径也要补齐,
     否则下一轮请求直接 400(OpenAI 协议硬约束); Ctrl+C 也走 _fixup 补齐后再收工.
  2. 工具错误一律变 error ToolRet 回喂模型自纠(tools.py 的约定), 本层不再捕获.
  3. payload 先 build 后 send, 中间的缝落 trace —— 将来实验旋钮在这里注入.

M1 增: session 挂钩 —— 每条进 messages 的消息同步落 transcript(断电可续);
preload 参数支持 resume 时把旧会话消息整体搬进来(不再注系统提示词, 旧会话自带).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import instr
import hooks as hooks_mod
import knobs as knobs_mod
import prompts as prompts_mod
import session as session_mod
import tools
import vision as vision_mod
from instr import payload_sha1, sha1_text

MAX_ITERS = 25      # 熔断: 防模型无限调工具烧钱
RETRIES = 2         # provider.send 失败重试次数(共 1+RETRIES 次尝试)
BACKOFF = 2.0       # 退避基数: 2s, 4s

COMPACT_MIN_CHARS = 2000            # 会话小于此规模不压(摘要也花钱, 不做亏本买卖)
AUTOCOMPACT_ENV = "AT_AUTOCOMPACT_TOK"
AUTOCOMPACT_DEFAULT = 80000         # 默认水位线(prompt tokens); 0=关闭

SUB_MAX_ITERS = 12      # M14: 分身的迭代熔断(父是 25 —— 子是一次性短任务)
SUB_PARALLEL_MAX = 4    # M14: 一条消息里连续 spawn 的并发上限
SUB_RESULT_CAP = 4000   # M14: 分身结论回传字符上限(超出截断, 防结论挤爆父上下文)
SUB_PROMPT_CAP = 8000   # M14: 派活 prompt 字符上限
SUB_JOIN_POLL = 0.2     # M14: 并行段等待/中断联动轮询粒度(同 ask_perm 逃生口家法)

SUMMARIZER_PROMPT = (
    "你是会话压缩器. 把给定的 agent 对话历史压缩成一份'接续摘要', 供同一个 agent "
    "无损接续工作. 必须保留: 用户的目标与偏好指示 / 已做的关键决定及其理由 / "
    "涉及到的文件路径与命令 / 工具核实过的事实与数字 / 未完成事项与下一步. "
    "丢弃: 寒暄 / 失败尝试的细节(只留'试过X不行'一句) / 工具原始输出的原文. "
    "用中文条目式输出, 不超过 1200 字, 不要调用任何工具."
)

MEMORY_DIGEST_PROMPT = (
    "你是记忆整理器. 根据本轮对话与现有记忆卡清单, 判断有没有值得跨会话长期保留的"
    "新事实或对既有卡的更新 —— 用户偏好与长期指示 / 项目背景与关键决定 / 本轮核实过"
    "的稳定事实. 只在确有价值时输出, 格式严格:\n"
    "【记忆】标题(与现有卡同名 = 更新那张卡)\n"
    "内容(可多行, 写给未来会话读)\n"
    "【/记忆】\n"
    "可输出多个块; 没有值得存的只回\"无\". 不要存: 临时任务细节 / 一次性上下文 / 寒暄. "
    "新信息若是某张现有卡的同主题延续, 尽量沿用原卡名(同名=更新那张), 别另造新名. "
    "**只有内容确实是某张现有卡的同主题延续才更新它; 无关的新事实(包括本轮成果)起自己的"
    "新卡名 —— 绝不把不相干内容塞进现有卡(2026-10-13 实锤: 曾把流水线说明塞进课程背景卡).**"
)

# 整理倾向(设置页「轮末记忆的激进度」可调 = 生成记忆卡的"阈值"), 拼在提示词尾部.
# 实测背景(2026-10-13): 标准档把"让 agent 拼了条流水线"判成一次性任务不落卡, 用户要阀钮.
MEMORY_DIGEST_LEVELS = {
    "quiet": "\n你的倾向: 保守 —— 只记明确长期有效的事实(偏好/约定/项目背景/关键决定), "
             "任务过程与一次性操作一律不记, 拿不准就不记.",
    "standard": "",
    "eager": "\n你的倾向: 积极 —— 本轮做过的、用户将来可能再问到的事都值得记: 任务成果与"
             "流程(如生成了什么流水线/怎么做的)、项目进展、工具与配置的选择、用户的工作"
             "习惯; 只排除寒暄与纯闲聊.",
}

_MEM_BLOCK = re.compile(r"【记忆】(.+?)\n(.*?)【/记忆】", re.S)


def parse_memory_blocks(text):
    """整理器输出 → [(标题, 内容)]; 不含块/空块 → []."""
    out = []
    for m in _MEM_BLOCK.finditer(text or ""):
        title, content = m.group(1).strip(), m.group(2).strip()
        if title and content:
            out.append((title, content))
    return out


def _render_for_summary(messages):
    """把消息列渲染成给摘要器的纯文本(tool_calls/长内容都裁剪)."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if role == "tool":
            content = (m.get("content") or "")[:300]
            lines.append("tool[%s]: %s" % (m.get("tool_call_id", ""), content))
            continue
        text = (m.get("content") or "")[:2000]
        calls = m.get("tool_calls") or []
        if calls:
            rendered = "; ".join("%s(%s)" % (
                (c.get("function") or {}).get("name", ""),
                ((c.get("function") or {}).get("arguments") or "")[:200])
                for c in calls)
            text = (text + " " if text else "") + "[调用 " + rendered + "]"
        lines.append("%s: %s" % (role, text))
    return "\n".join(lines)


class AbortFlag:
    """Ctrl+C 的载体. M0 是骨架: 循环点检查; M1 的 REPL 由 KeyboardInterrupt 兜底."""

    def __init__(self):
        self.stop = False

    def set(self):
        self.stop = True


class SubBusProxy:
    """M14: 分身事件打标中转 —— 给子循环的每个事件注入 sub/sub_label, 让它落进父
    trace 时能归属到哪个分身. 只包 emit(全仓 bus 调用面仅 emit; 无 sink 读 ev
    ["session"]), add_sink/session 转发只为封死意外用法.

    注意: sub/sub_label 必须是普通键 —— jsonl_sink 会剥掉下划线前缀键, 研究字段
    一旦写成 _sub 就全丢."""

    def __init__(self, bus, sid, label):
        self._bus, self._sid, self._label = bus, sid, label

    @property
    def session(self):
        return self._bus.session

    def add_sink(self, sink):
        return self._bus.add_sink(sink)

    def emit(self, type_, **data):
        if "sub" in data:      # 已带 sub 打标(嵌套代理链: 分身包分身/流水线节点派分身)
            return self._bus.emit(type_, **data)   # 内层身份保留, 外层不重复打标
        self._bus.emit(type_, sub=self._sid, sub_label=self._label, **data)


def build_sub_sysprompt(cwd, registry, provider="", model="", agent_md=None):
    """M14: 子代理系统提示词 = 完整环境提示词(含工具清单/AGENT.md) + SUB_RULES.

    模块级函数 = 测试打桩缝: 真实构建含日期/环境块, golden 会跨天漂移; 场景与自检
    里替换为常量(先例: version.WATCH_BASE / mcpclient.boot_mcp)."""
    return (prompts_mod.build_sysprompt(cwd, registry=registry, provider=provider,
                                        model=model, agent_md=agent_md)
            + "\n\n" + prompts_mod.SUB_RULES)


# M28: explore 只读探查分身的追加约束(接在子系统提示词尾, 近因最强)
EXPLORE_SP = (
    "\n\n【只读探查分身】你是只读探查代理: 只调研取证(读文件/搜代码/查网页), 不写文件"
    "不修改任何东西; 结论要带证据(路径/行号/引用). 你没有 write/edit 工具是设计如此, "
    "不要尝试.")


class TurnSummary:
    def __init__(self, iterations, usage_total, aborted, final_text, error=""):
        self.iterations = iterations
        self.usage_total = usage_total
        self.aborted = aborted
        self.final_text = final_text
        self.error = error


class AgentLoop:
    def __init__(self, provider, registry, bus, sysprompt="", max_iters=MAX_ITERS,
                 cwd=None, session=None, preload=None, perms=None, knobs=None,
                 todos=None, stream=False, auto_memory=False, ask_user=None,
                 platform=None, memory_level="standard", memory_overwrite=None,
                 sub_provider_fn=None, sub_models=None, sub_allow_other=None,
                 image_gen=None, hooks=None, sub_vision_models=None):
        self.provider = provider
        self.registry = registry
        self.bus = bus
        self.max_iters = max_iters
        self.stream = stream   # M6: SSE 流式(loop 级开关; compact 摘要调用恒非流式)
        self.auto_memory = auto_memory   # M9.3: 轮末自动记忆(前端默认开; runlab 恒关)
        self.memory_level = memory_level   # 轮末记忆激进度: quiet/standard/eager(设置页可调)
        self.memory_overwrite = memory_overwrite   # 同名覆盖策略: ask/keep/auto 或 callable
        self.knobs = knobs if knobs is not None else {}   # 同一 dict 引用: REPL /knob 原地改
        self.abort = AbortFlag()
        self.perms = perms
        self.hooks = hooks   # M25: {pre_tool:[..],post_tool:[..]} 快照; None=不挂
        self.ctx = tools.ToolCtx(cwd=cwd or os.getcwd(), abort=self.abort, perms=perms,
                                 bus=bus, session=session, ask_user=ask_user,
                                 platform=platform, memory_overwrite=memory_overwrite,
                                 sub_provider_fn=sub_provider_fn, sub_models=sub_models,
                                 sub_allow_other=sub_allow_other, hooks=hooks,
                                 sub_vision_models=sub_vision_models)
        self.ctx.spawn = self._spawn_one   # M14: 循环自己实现分身(工具层不知道 loop)
        self.set_image_gen(image_gen)      # M15: 画图解析器(前端注入); 绑方法挂进 ctx
        self.ctx.look_image = self._look_image_one   # M32: 识图单发(本地图→视觉档模型)
        # M14 分身状态: seq/children 带锁(并行段多线程写); registry 视图与 AGENT.md 惰性一次
        self._sub_seq = 0
        self._sub_lock = threading.Lock()
        self._sub_children = {}          # sid -> child loop(中断联动 _abort_children)
        self._sub_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._sub_reg = None             # 惰性: 滤掉 spawn 的 registry 视图
        self._sub_md = None              # 惰性: AGENT.md 发现结果(每父一次, N 个子不重走)
        if todos:
            self.ctx.todos = list(todos)    # M7: resume/compact 恢复任务清单
        self.session = session
        self.use_cache = os.environ.get("AT_CACHE") == "1"   # 显式回放模式, 默认关
        try:
            self.autocompact_tokens = int(os.environ.get(AUTOCOMPACT_ENV,
                                                         AUTOCOMPACT_DEFAULT))
        except ValueError:
            self.autocompact_tokens = AUTOCOMPACT_DEFAULT
        self._last_prompt_tokens = 0        # 水位线依据: 最近一次请求的 prompt_tokens
        self.messages = []
        if preload:
            self.messages = list(preload)   # resume: 旧会话消息原样搬入, 不再注系统提示词
        elif sysprompt:
            self._append({"role": "system", "content": sysprompt})

    # ------ Message bookkeeping ------
    def _append(self, msg):
        self.messages.append(msg)
        if self.session is not None:
            try:
                self.session.append("message", msg)
            except Exception:
                pass  # transcript 落盘失败不伤主流程

    # ------ Main loop ------
    def run_turn(self, user_text, images=None):
        # 中断旗只该管当轮: 开工复位 —— 否则中断过一次后本窗每轮都在循环头瞬间判
        # aborted(先于本功能就存在的病, 2026-10-13 修; ask/platform 的等待逃生口也吃它)
        self.abort.stop = False
        # M7 水位管理: 上一轮请求超线则先压缩再接新话(每轮最多一次, 压不下去也继续)
        if self.autocompact_tokens and self._last_prompt_tokens >= self.autocompact_tokens:
            stats, err = self.compact()
            self.bus.emit("auto_compact", stats=stats,
                          note=err or "auto compacted before turn")

        msg = {"role": "user", "content": user_text}
        if images:
            msg["_images"] = list(images)   # M32: 识图委派进图缝(api_messages 按视觉档喂)
        self._append(msg)
        self.bus.emit("turn_start", user_text=user_text)

        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        final_text, error, aborted = "", "", False
        iterations = 0

        try:
            while iterations < self.max_iters:
                if self.abort.stop:
                    aborted = True
                    break

                iterations += 1
                if iterations == self.max_iters:
                    # 熔断: 最后一轮强制要结论, 不再给工具发挥空间
                    self._append({"role": "user",
                                  "content": "[系统提示] 已达到迭代上限, 停止调用工具, 直接给出最终结论."})

                result = self._call_llm()
                if result is None:
                    error = self.provider.last_error
                    # 补一条 assistant 消息兜住对话结构, 避免 transcript 尾部悬挂 tool 消息
                    self._append({"role": "assistant",
                                  "content": "(llm 调用失败, 本轮中止: %s)" % error})
                    break
                self._last_prompt_tokens = (result.usage or {}).get("prompt_tokens") or 0

                self._append(result.message)
                for k in usage_total:
                    usage_total[k] += (result.usage or {}).get(k, 0) or 0

                calls = result.message.get("tool_calls") or []
                if not calls:
                    final_text = result.message.get("content") or ""
                    break

                if self._run_tool_calls(calls):
                    aborted = True
                    break
        except KeyboardInterrupt:
            # Ctrl+C: transcript 结构完整性优先(红线 1), 补齐配对再收工
            aborted = True
            self._fixup_after_interrupt()

        with self._sub_lock:   # M14: 分身消耗并账 —— 缺它 REPL 落款/one-shot 汇总会漏计分身
            for k in self._sub_usage:
                usage_total[k] += self._sub_usage.get(k, 0) or 0
                self._sub_usage[k] = 0

        summary = TurnSummary(iterations, usage_total, aborted, final_text, error)
        self.bus.emit("turn_end", iterations=iterations, usage_total=usage_total,
                      aborted=aborted, error=error)
        if self.auto_memory and not aborted and final_text:
            try:
                self._memory_digest()
            except Exception:
                pass        # 记忆整理崩了不能伤主流程
        return summary

    # ------ 轮末自动记忆(M9.3) ------
    def _memory_digest(self):
        """每轮回复完用户后自动整理记忆: 一次小调用(无工具不注旋钮, 同 compact
        摘要的'内部机械处置'口径)提取本轮值得长期保留的事实 → 写卡(同名覆写=更新).
        事件全带 internal="memory_digest" 标记: UI 不渲染, trace 可分辨(研究口径);
        每张卡照发 memory_write 事件 → 聊天流"已存记忆卡"提示与卡列刷新复用同一条路."""
        import memory as memory_mod
        try:
            cards = memory_mod.list_cards()
        except Exception:
            cards = []
        titles = ", ".join(c["title"] for c in cards) or "(空)"
        user_text = ""
        for m in reversed(self.messages):
            if m.get("role") == "user":
                user_text = (m.get("content") or "")[:1500]
                break
        final = ""
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and m.get("content"):
                final = m["content"][:1500]
                break
        lvl = self.memory_level                    # 字符串或 callable(设置页实时)
        if callable(lvl):
            try:
                lvl = lvl() or "standard"
            except Exception:
                lvl = "standard"
        msgs = [{"role": "system",
                 "content": MEMORY_DIGEST_PROMPT + MEMORY_DIGEST_LEVELS.get(lvl, "")},
                {"role": "user",
                 "content": "现有记忆卡: %s\n\n本轮对话:\n用户: %s\n\n助手最终回复: %s"
                            % (titles, user_text, final)}]
        payload = self.provider.build_payload(msgs, tools=None)
        self.bus.emit("llm_request", provider=self.provider.name,
                      model=self.provider.model, payload=payload,
                      payload_sha1=payload_sha1(payload),
                      internal="memory_digest")
        result = self.provider.send(payload)
        if result is None:
            self.bus.emit("memory_digest", cards=[], err=self.provider.last_error)
            return
        self.bus.emit("llm_response", provider=self.provider.name,
                      model=self.provider.model, message=result.message,
                      usage=result.usage, finish_reason=result.finish_reason,
                      latency_ms=result.latency_ms, cached=result.cached,
                      _raw=result.raw, internal="memory_digest")
        written = []
        vers = {c["title"]: memory_mod.card_version(c["title"]) for c in cards}   # 内容 sha1 基线
        mo = self.memory_overwrite                  # 设置页「同名覆盖策略」
        if callable(mo):
            try:
                mo = mo() or "ask"
            except Exception:
                mo = "ask"
        mo = mo or "ask"
        for title, content in parse_memory_blocks(result.message.get("content") or ""):
            tc = memory_mod.norm_title(title)
            kept = False
            if tc and tc in vers and mo == "keep":   # 设置"不覆盖": 同名(哪怕没撞车)也另存
                kept = True
            expect = vers.get(tc, "") if tc else None    # 见过=认那一版; 没见过=不许已存在
            if kept:
                t, why = "", "raced"                     # 走同一条另存路
            else:
                t, why = memory_mod.write_card_guarded(title, content, expect_version=expect)
            if why == "raced" and tc:
                # 撞车(它刚改了同主题卡/刚占了这个新卡名)或设置"不覆盖": 绝不盲盖 —— 另存
                cand, n = "%s（更新）" % tc, 2
                while memory_mod.card_exists(cand):
                    cand = "%s（更新%d）" % (tc, n)
                    n += 1
                t = memory_mod.write_card(cand, content)
                if t:
                    extra = {"kept": True, "kept_from": tc} if kept else \
                        {"raced": True, "raced_from": tc}
                    self.bus.emit("memory_write", title=t, overwritten=False,
                                  src="digest", **extra)
                    written.append(t)
                continue
            if t:
                ov = any(c["title"] == t for c in cards)
                # 疑似重复提示(机械, 零额外 LLM 调用): 非同名覆写时, 与现有卡
                # +本轮先前写入的卡做卡名近似判定 → 事件带 near, UI 出一行提示
                near = [] if ov else [n for n in
                                      [c["title"] for c in cards] + written
                                      if memory_mod.similar_title(t, n)]
                kw = {"near": near} if near else {}
                self.bus.emit("memory_write", title=t, overwritten=ov,
                              src="digest", **kw)
                written.append(t)
        self.bus.emit("memory_digest", cards=written, usage=result.usage or {})

    # ------ Compact (M5 落地于 at.py, M7 下沉为本方法: 手动/自动共用一条路) ------
    def compact(self, min_chars=COMPACT_MIN_CHARS):
        """上下文压缩, messages **原地**替换(自动/手动共用; loop 引用不变):

        摘要调用(无工具, 不注旋钮=内部机械非实验处置, 事件走本总线照常记账) →
        新消息 = [重建的系统提示词(AGENT.md 重新发现), 前情摘要] →
        transcript 轮转(旧档改名 .pre-compact 保留, 同 sid 新文件记 compact meta
        并重放压缩后消息与当前 todo 清单). 返回 (stats, ""); 失败 (None, 原因)."""
        msgs = self.messages
        chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
        if len(msgs) < 4 or chars < min_chars:
            return None, "会话还小(%d 条 / %d 字符 < %d), 不值得压缩" % (
                len(msgs), chars, min_chars)

        sum_msgs = [{"role": "system", "content": SUMMARIZER_PROMPT},
                    {"role": "user",
                     "content": "把以下 agent 对话历史压缩成接续摘要:\n\n"
                                + _render_for_summary(msgs)}]
        payload = self.provider.build_payload(sum_msgs, tools=None)
        sha = payload_sha1(payload)
        self.bus.emit("llm_request", provider=self.provider.name,
                      model=self.provider.model, payload=payload, payload_sha1=sha)
        result = self.provider.send(payload)
        if result is None:
            return None, "摘要调用失败: %s (会话未动)" % self.provider.last_error
        self.bus.emit("llm_response", provider=self.provider.name,
                      model=self.provider.model, message=result.message,
                      usage=result.usage, finish_reason=result.finish_reason,
                      latency_ms=result.latency_ms, cached=result.cached,
                      _raw=result.raw)
        summary = (result.message.get("content") or "").strip()
        if not summary:
            return None, "摘要为空, 放弃压缩 (会话未动)"

        sp = prompts_mod.build_sysprompt(
            self.ctx.cwd, registry=self.registry,
            provider=self.provider.name, model=self.provider.model,
            agent_md=prompts_mod.discover_agent_md(self.ctx.cwd))
        new_msgs = [{"role": "system", "content": sp},
                    {"role": "user",
                     "content": "[前情摘要] 以下是此前会话的机器压缩, 事实以此为准, "
                                "从这里继续协助用户:\n\n" + summary}]
        new_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in new_msgs)

        rotated = ""
        if self.session is not None:
            # 轮转: 先关句柄(Windows 打开中的文件不能 rename), 旧档原样保留
            self.session.close()
            rot = self.session.path + ".pre-compact"
            n = 1
            while os.path.exists(rot):
                n += 1
                rot = "%s.pre-compact-%d" % (self.session.path, n)
            os.rename(self.session.path, rot)
            new_sess = session_mod.Session(self.session.sid, self.session.path)
            new_sess.meta(event="compact", before_messages=len(msgs), before_chars=chars,
                          after_messages=len(new_msgs), after_chars=new_chars,
                          rotated_from=os.path.basename(rot))
            for m in new_msgs:
                new_sess.append("message", m)
            for t in self.ctx.todos:          # todo 续传: 清单搬进新档
                new_sess.append("todo", t)
            self.session = new_sess
            self.ctx.session = new_sess
            rotated = rot

        self.messages = list(new_msgs)
        self._last_prompt_tokens = 0          # 压缩后水位重新计
        return {"before": (len(msgs), chars), "after": (len(new_msgs), new_chars),
                "rotated": rotated, "usage": result.usage or {}}, ""

    # ------ Pieces ------
    def _call_llm(self):
        """build → trace → send(带退避重试). 发出去的 payload 与回来的响应都全量落事件.
        AT_CACHE=1 时先查回放缓存(同 payload 命中历史响应, 不发网络, usage 记账单列).
        stream=True 时 send 带 on_chunk: 增量走 llm_chunk 事件(UI 逐字呈现),
        llm_response 仍发组装后的规范形(transcript/记账/golden 口径不变)."""
        # M15 视觉缝: canonical messages → 发送视图(视觉档在本轮 tool 块后注入图像
        # user 消息; 非视觉档剥引用+注记) —— 每次请求现造, self.messages 永不动。
        msgs_api = vision_mod.api_messages(
            self.messages, bool(getattr(self.provider, "vision", False)))
        payload = self.provider.build_payload(
            msgs_api, tools=self.registry.schemas() or None)
        # M6 旋钮缝: build 与 send 之间注入 —— payload 带旋钮落 trace,
        # record 并进事件, 研究扫描不用重解 payload。
        payload, knob_record = knobs_mod.apply(payload, self.knobs,
                                               getattr(self.provider, "family", ""))
        # M15 红线 4: trace 里的图像本体 → "media:<sha1>" 占位(原件不动, send 发真的);
        # sha1 按脱敏后算(=确定性 + 不同图不同键, 且不用对几 MB 字符串做哈希)
        payload_ev, img_refs = vision_mod.scrub_images(payload)
        sha = payload_sha1(payload_ev)
        emit_kw = {"images": img_refs} if img_refs else {}
        if self.stream:
            emit_kw["stream"] = True   # 事件级标注; payload 不动(sha1 与回放口径稳定)
        self.bus.emit("llm_request", provider=self.provider.name,
                      model=self.provider.model, payload=payload_ev,
                      payload_sha1=sha, knobs=knob_record, **emit_kw)
        if self.use_cache:
            hit = instr.cache_get(sha)
            if hit is not None:
                try:
                    result = self.provider._parse(hit, 0)
                    result.cached = True
                except Exception:
                    result = None
                if result is not None:
                    self.bus.emit("llm_response", provider=self.provider.name,
                                  model=self.provider.model, message=result.message,
                                  usage=result.usage, finish_reason=result.finish_reason,
                                  latency_ms=0, cached=True, _raw=hit)
                    return result
        emitted = [0]                  # 已吐出的流式增量数

        def on_chunk(kind, text):
            if self.abort.stop:          # 中断逃生口(2026-10-20): 流式半途拔线
                raise KeyboardInterrupt()  # —— KI 是 BaseException, provider 的
            emitted[0] += 1              # except Exception 吞不掉, run_turn 接住
            self.bus.emit("llm_chunk", kind=kind, text=text)

        for attempt in range(RETRIES + 1):
            result = self.provider.send(payload,
                                        on_chunk=on_chunk if self.stream else None)
            if result is not None:
                if self.use_cache:
                    instr.cache_put(sha, result.raw)
                self.bus.emit("llm_response", provider=self.provider.name,
                              model=self.provider.model, message=result.message,
                              usage=result.usage, finish_reason=result.finish_reason,
                              latency_ms=result.latency_ms, cached=result.cached,
                              _raw=result.raw)
                return result
            self.bus.emit("error", where="provider.send",
                          repr_=self.provider.last_error, attempt=attempt)
            if emitted[0]:
                break   # 流式半途而废: 正文已吐一半, 重发会重复 —— 本轮直接失败
            if attempt < RETRIES:
                time.sleep(BACKOFF * (2 ** attempt))
        return None

    def _run_tool_calls(self, calls) -> bool:
        """执行并回填 tool_call. 返回 True = 被中断.

        红线 1: 每个 call_id 恒配对一条 tool 消息 —— 中断/未执行的也补 "aborted by
        user"(旧实现在 abort 落于 calls 中间时留孤儿 id, 下轮请求 400; 2026-10-13 补齐).
        M14: 连续全 spawn 段(单条也算, 每次至多 SUB_PARALLEL_MAX 条)并发跑; 回填恒按
        calls 原序; 其余工具照旧顺序执行."""
        i, n = 0, len(calls)
        while i < n:
            if self.abort.stop:
                self._fill_aborted(calls[i:])
                return True
            name = ((calls[i].get("function") or {}).get("name") or "")
            if name == "spawn":
                j = i
                while (j < n and j - i < SUB_PARALLEL_MAX and
                       ((calls[j].get("function") or {}).get("name") or "") == "spawn"):
                    j += 1
                if self._run_segment(calls, list(range(i, j))):
                    self._fill_aborted(calls[j:])
                    return True
                i = j
                continue
            if self._run_one(calls[i]):
                self._fill_aborted(calls[i + 1:])
                return True
            i += 1
        return False

    def _run_one(self, tc) -> bool:
        """顺序路单条执行(非 spawn 工具). 返回 True = 被中断."""
        call_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_raw = fn.get("arguments") or ""
        self.bus.emit("tool_call", name=name, args_raw=args_raw, call_id=call_id)
        t0 = time.time()
        ret = self._gate_and_dispatch(name, args_raw)
        duration_ms = int((time.time() - t0) * 1000)
        img_kw = {"images": ret.images, "n_images": len(ret.images)} \
            if getattr(ret, "images", None) else {}
        self.bus.emit("tool_result", call_id=call_id, ok=ret.ok,
                      content_len=len(ret.content), content_sha1=sha1_text(ret.content),
                      duration_ms=duration_ms, truncated=ret.truncated,
                      _content=ret.content, **img_kw)
        # 红线 1: 无论 ok/err/中断, 每个 call_id 都配对一条 tool 消息
        msg = {"role": "tool", "tool_call_id": call_id, "content": ret.content}
        if getattr(ret, "images", None):
            msg["_images"] = ret.images   # M15: 引用随消息走(本体在媒体池);
                                          # 发送视图按模型能力决定附/略(见 vision.api_messages)
        self._append(msg)
        return self.abort.stop

    def _fill_aborted(self, rest):
        """红线 1 兜底: 被中断而没轮到的 tool_call 也要配对, 否则下轮请求 400.
        同 _fixup_after_interrupt 口径: 只补消息, 不补事件."""
        for tc in rest:
            self._append({"role": "tool", "tool_call_id": tc.get("id") or "",
                          "content": "aborted by user"})

    # ------ M14: spawn 段并行 ------
    def _run_segment(self, calls, idxs) -> bool:
        """一段 spawn 并发跑(daemon 线程 + 父轮询中断联动). 返回 True = 被中断.

        父面事件序确定(全段 tool_call 原序先发 → tool_result 原序后发); 子面事件在各自
        线程交织(带 sub 标记). 中断时短宽限后不再等慢子 —— 已付费的请求在后台收尾,
        结果如实写"未回". 不用 ThreadPoolExecutor: 池线程非 daemon, 退出/Ctrl+C 会被
        shutdown(wait=True) 挂到子请求超时(120s), daemon+轮询是本库现成家法."""
        for k in idxs:                     # ① 全段先发调用块(原序)
            fn = calls[k].get("function") or {}
            self.bus.emit("tool_call", name=fn.get("name") or "",
                          args_raw=fn.get("arguments") or "",
                          call_id=calls[k].get("id") or "")
        results, threads = {}, []
        for k in idxs:                     # ② 每 call 一条 daemon 线程
            th = threading.Thread(target=self._sub_worker, args=(calls[k], k, results),
                                  daemon=True, name="at-sub-%d" % k)
            threads.append(th)
            th.start()
        # ③ 等待 + 中断联动: 反复置子 abort —— 盖住"子刚开工, run_turn 头会复位标志"的窗口
        interrupted = False
        try:
            while True:
                if self.abort.stop:
                    interrupted = True
                if interrupted:
                    self._abort_children()
                alive = [th for th in threads if th.is_alive()]
                if not alive:
                    break
                for th in alive:
                    th.join(SUB_JOIN_POLL)
                if interrupted:
                    break              # 短宽限已给; 慢子不阻塞收工(后台收尾, 账不丢)
        except KeyboardInterrupt:      # Ctrl+C 只落主线程: 与中断同口径收口, 不外抛
            interrupted = True
            self._abort_children()
        if interrupted:
            self._abort_children()     # 最后一发: 盖宽限期内才进 run_turn 的子
        for k in idxs:                 # ④ 恒按 calls 原序回填(红线 1)
            ret = results.get(k) or tools.ToolRet(
                False, "分身被中断, 结果未回(子代理可能仍在后台收尾)")
            t0 = results.get(("t0", k)) or time.time()
            img_kw = {"images": ret.images, "n_images": len(ret.images)} \
                if getattr(ret, "images", None) else {}
            self.bus.emit("tool_result", call_id=calls[k].get("id") or "", ok=ret.ok,
                          content_len=len(ret.content),
                          content_sha1=sha1_text(ret.content),
                          duration_ms=int((time.time() - t0) * 1000),
                          truncated=ret.truncated, _content=ret.content, **img_kw)
            msg = {"role": "tool", "tool_call_id": calls[k].get("id") or "",
                   "content": ret.content}
            if getattr(ret, "images", None):
                msg["_images"] = ret.images   # M15: 同上(引用随消息走)
            self._append(msg)
        return interrupted or self.abort.stop

    def _sub_worker(self, tc, k, results):
        """段内单条 spawn 的线程体: 只过门+分发(与主路同一条权限门). 异常兜底在
        gate/dispatch 层, 本层再兜一次 —— 线程体绝不外抛(否则结果永远填不上)."""
        results[("t0", k)] = time.time()
        fn = tc.get("function") or {}
        try:
            results[k] = self._gate_and_dispatch(fn.get("name") or "",
                                                 fn.get("arguments") or "")
        except Exception as ex:
            results[k] = tools.ToolRet(False, "spawn 线程崩: %s: %s"
                                       % (type(ex).__name__, ex))

    def _abort_children(self):
        """把父的中断旗传给所有在跑的子(子各有自己的 AbortFlag —— 不能共享父的:
        子的 run_turn 头会复位它)."""
        with self._sub_lock:
            kids = list(self._sub_children.values())
        for c in kids:
            try:
                c.abort.set()
            except Exception:
                pass

    # ------ M14: 分身(子代理) ------
    def _spawn_one(self, prompt, label="", model="", agent_type="general", inherit=0,
                   images=None):
        """派一个分身子代理跑任务, 结论以 ToolRet 回给父模型. exec 在段线程里
        (见 _run_segment)—— 单条 spawn 也走线程, 父线程才能轮询中断.
        M28: agent_type=explore → 只读探查视图(无 write/edit)+只读系统词;
        inherit=N → 主对话最近 N 条 user/assistant 以 system 注记带给分身当背景;
        M32: images=[本地图路径] → 识图分身(图随任务进子对话, 须挑视觉档模型)."""
        if self.ctx.sub:
            return tools.ToolRet(False, "分身不能再派分身(深度上限 1 层) —— 自己把活干完.")
        if self.abort.stop:
            return tools.ToolRet(False, "父代理已中断, 分身未启动.")
        explore = agent_type == "explore"
        prompt = prompt.strip()[:SUB_PROMPT_CAP]
        img_refs = []                            # M32: 识图分身 —— 图随任务进子对话
        if images:
            if len(images) > vision_mod.IMG_MAX_PER_TURN:
                return tools.ToolRet(
                    False, "spawn: 一次最多带 %d 张图 —— 多图就多派几个分身, 各看各的"
                           "图各写各的结论, 你来汇总." % vision_mod.IMG_MAX_PER_TURN)
            for p in images:
                ref = vision_mod.ref_from_path(tools._resolve(self.ctx, str(p)))
                if ref is None:
                    return tools.ToolRet(
                        False, "spawn: 读不到图片文件 %r —— 检查路径(相对路径相对当前"
                               "工作目录; 截图/用户拖进来的图都是路径)." % p)
                img_refs.append(ref)
            label = label or "识图"
        prov, who = self.provider, "%s/%s" % (self.provider.name, self.provider.model)
        if model:
            fn = self.ctx.sub_provider_fn
            if fn is None:
                return tools.ToolRet(
                    False, "spawn: 本环境不支持指定分身模型(%r) —— 省略 model 用当前"
                           "模型, 或到前端聊天窗里跑." % model)
            allow = self.ctx.sub_allow_other      # 设置页开关(live callable); 默认关
            try:
                allow = bool(allow() if callable(allow) else allow)
            except Exception:
                allow = False
            if not allow:
                return tools.ToolRet(
                    False, "spawn: 分身只允许用当前对话模型(设置→分身「允许用非当前模型」"
                           "未开) —— 省略 model 用当前模型即可; 想换模型请让用户去设置里开.")
            try:
                prov2 = fn(model)
            except Exception:
                prov2 = None
            if prov2 is None:                      # 点名了却解析不到: 回喂候选让模型自纠
                sm = self.ctx.sub_models
                try:
                    names = list(sm() if callable(sm) else sm) if sm else []
                except Exception:
                    names = []
                tail = ("当前可用的: " + ", ".join(names[:20])) if names else \
                    "先确认该 agent 已加入「我的模型」且 key 可用"
                return tools.ToolRet(
                    False, "spawn: 清单里没有可用的 %r(写法=家/型号, 如 "
                           "deepseek/deepseek-v4-pro) —— %s." % (model, tail))
            prov, who = prov2, model
        with self._sub_lock:
            self._sub_seq += 1
            sid = "s%d" % self._sub_seq
        label = label or sid
        reg = self._sub_registry(readonly=explore)
        # 默认子与父共用同一个 provider 对象(不 copy —— copy 会连 FakeProvider 的剧本
        # 步进一起复制毁回放; Provider.send 只读配置, 并发无害).
        sub_sp = build_sub_sysprompt(self.ctx.cwd, reg, prov.name, prov.model,
                                     self._sub_agent_md())
        if explore:
            sub_sp += EXPLORE_SP
        child = AgentLoop(
            prov, reg, SubBusProxy(self.bus, sid, label),
            sysprompt=sub_sp,
            max_iters=SUB_MAX_ITERS, cwd=self.ctx.cwd, session=None,   # 不落 transcript
            perms=self.perms,          # 同一道门: 内层写/执行照弹框确认
            knobs=self.knobs, stream=False, auto_memory=False,
            ask_user=self._sub_ask(label),   # 头加 [标签] 前缀, 答案回各自线程
            platform=None,                   # 分身不直控界面(platform 工具优雅降级)
            image_gen=self._image_gen_resolver,   # M15: 分身也能画图(解析器透传)
            hooks=self.hooks)                # M25: 分身同吃 pre/post 钩子
        child.ctx.sub = True                     # 深度守卫: 子不能再派
        child.ctx.sub_provider_fn = None         # 子无模型解析器
        if inherit > 0:                          # M28: 继承主对话最近 N 条当背景
            # 只数"有内容"的 user/assistant(当前轮的空 assistant=tool_call 消息
            # 不占名额), 取尾部 N 条 —— inherit=N 就是真给分身看 N 条话.
            picked = [m for m in self.messages
                      if m.get("role") in ("user", "assistant")
                      and str(m.get("content") or "").strip()][-int(inherit):]
            lines = ["%s: %s" % ("用户" if m.get("role") == "user" else "助手",
                                 re.sub(r"\s+", " ",
                                        str(m.get("content"))[:600]))
                     for m in picked]
            if lines:
                child._append({
                    "role": "system",
                    "content": "【继承的对话上下文(主对话最近 %d 条, 只作背景了解, "
                               "不是给你的任务)】\n%s" % (len(lines), "\n".join(lines))})
        child.autocompact_tokens = 0             # 一次性上下文: 不做隐式摘要(防隐形花费)
        with self._sub_lock:
            self._sub_children[sid] = child
        t0 = time.time()                          # M19: 分包墙钟(账本用)
        try:
            s = child.run_turn(prompt, images=img_refs or None)
        except Exception as ex:
            return tools.ToolRet(False, "分身崩了: %s: %s" % (type(ex).__name__, ex))
        finally:
            with self._sub_lock:
                self._sub_children.pop(sid, None)
        with self._sub_lock:                     # 账目并回父轮(见 run_turn 收尾)
            for k in self._sub_usage:
                self._sub_usage[k] += (s.usage_total or {}).get(k, 0) or 0
        ok = not s.aborted and not s.error
        body = (s.final_text or "").strip()
        # M19 分包账本: 每笔分包记一条客观账(失败静默 —— 账本绝不能伤 spawn).
        # 发起会话取父 sid; runflow 节点分身时 self.session 为 None, bus.session
        # 经 SubBusProxy 转发照样拿到发起聊天窗的 sid.
        try:
            import ledger as ledger_mod
            ledger_mod.append({
                "ts": t0,
                "sid": (self.session.sid if self.session is not None
                        else getattr(self.bus, "session", "")),
                "parent": "%s/%s" % (self.provider.name, self.provider.model),
                "sub": label, "model": who,
                "task": prompt[:ledger_mod.TASK_HEAD],
                "ok": ok,
                "tokens": {"prompt_tokens": s.usage_total.get("prompt_tokens", 0),
                           "completion_tokens": s.usage_total.get("completion_tokens", 0),
                           "total_tokens": s.usage_total.get("total_tokens", 0)},
                "wall_s": round(time.time() - t0, 2),
                "result_head": body[:ledger_mod.RESULT_HEAD]})
        except Exception:
            pass
        head = "[分身 %s · %s] 模型=%s · %d 轮 · in %s/out %s tokens" % (
            sid, label, who, s.iterations,
            s.usage_total.get("prompt_tokens", 0),
            s.usage_total.get("completion_tokens", 0))
        if s.error:
            head += " · 出错: %s" % str(s.error)[:120]
        if s.aborted:
            head += " · 被中断"
        if img_refs and not getattr(prov, "vision", False):
            head += (" · ⚠ 带图但该型号没勾「能看图」, 图没喂进去 —— 重派时点名视觉档"
                     "模型(「模型」页勾了「能看图」的条目)")
        text = head + "\n—— 分身结论 ——\n" + (body or "(分身没有给出结论)")
        return tools.ToolRet(ok, text[:SUB_RESULT_CAP], truncated=len(text) > SUB_RESULT_CAP)

    def _sub_registry(self, readonly=False):
        """子代理的工具视图: 排掉 spawn(深度守卫只在 dispatch 层, schemas()/系统提示词
        的工具清单也要干净 —— 否则模型白试一轮). readonly=True 再排 write/edit
        (M28 explore 只读探查分身, 对齐 CC Explore 只读代理; bash 保留 —— 命令仍过
        同一道 exec 门, 写操作会被门拦住)."""
        if readonly:
            return self.registry.view_of(exclude=("spawn", "write", "edit"))
        if self._sub_reg is None:
            self._sub_reg = self.registry.view_of(exclude=("spawn",))
        return self._sub_reg

    def _sub_agent_md(self):
        if self._sub_md is None:
            try:
                self._sub_md = prompts_mod.discover_agent_md(self.ctx.cwd)
            except Exception:
                self._sub_md = []
        return self._sub_md

    def _sub_ask(self, label):
        """子的 ask_user 包一层: 头部 [标签] 前缀 = 界面归属; 答案经各自回调回各自线程."""
        ask = self.ctx.ask_user
        if ask is None:
            return None          # REPL/批跑: 子的 ask_user 工具照旧优雅降级
        def wrapped(question, header, options):
            return ask(question, "[%s] %s" % (label, header or "分身提问"), options)
        return wrapped

    # ------ M15: 画图(图像生成) ------
    def set_image_gen(self, fn):
        """挂/换画图档解析器(前端注入 spec->{base_url,key,model}; None=本环境不可用).
        open_chat 与 resume/clear 重建后同步走这里(同 platform/ask_user 先例)."""
        self._image_gen_resolver = fn
        self.ctx.image_gen = self._image_gen_one if fn is not None else None

    def _image_gen_one(self, prompt, image_path, model):
        """image_gen 工具的执行: 解析画图档 → vision.run_image_gen → image_gen 事件.
        红线: key 绝不进事件(只报模型名/耗时/图元数据/错误)。"""
        spec = model or ""
        try:
            target = self._image_gen_resolver(spec) if self._image_gen_resolver else None
        except Exception:
            target = None
        if target is None:
            extra = ("点名 %r 没找到" % spec) if spec else "还没有勾了「能画图」的条目"
            return tools.ToolRet(
                False, "画图档解析失败(%s) —— 去「模型」页新增/编辑一个条目并勾选"
                       "「能画图」(官方家如智谱 cogview, 或中转站填 base_url/key)." % extra)
        t0 = time.time()
        ret = vision_mod.run_image_gen(target, prompt, image_path)
        seconds = round(time.time() - t0, 2)
        self.bus.emit("image_gen", model=target.get("model") or "",
                      prompt_len=len(prompt), image=bool(image_path), ok=ret.ok,
                      seconds=seconds,
                      images=[{k: r.get(k) for k in ("mime", "sha1", "bytes")}
                              for r in (getattr(ret, "images", None) or [])],
                      err="" if ret.ok else str(ret.content)[:200])
        return ret

    # ------ M32: 识图单发(look_image) ------
    def _vision_candidates(self):
        """视觉档候选清单: 前端接线 sub_vision_models(只列勾了「能看图」的条目名);
        没接(TUI/分身)退回 sub_models 全清单 —— 有名字可试总比空着强."""
        src = getattr(self.ctx, "sub_vision_models", None)
        try:
            names = list(src() if callable(src) else src) if src else []
        except Exception:
            names = []
        if names:
            return names
        sm = self.ctx.sub_models
        try:
            return list(sm() if callable(sm) else sm) if sm else []
        except Exception:
            return []

    def _look_image_one(self, path, question, model="", frames=2):
        """look_image 工具的执行: 本地图片/GIF/视频 → 媒体池 ref(s) → 单轮视觉调用
        (不带工具)回传文字结论. GIF 动图/视频抽帧(≤2)+元数据织进问题. 点名模型走
        分身解析器(同一道 sub_allow_other 门); 不点名用当前档(须视觉档).
        M19 账本照挂 —— 跨模型花钱得留客观账."""
        if self.abort.stop:
            return tools.ToolRet(False, "父代理已中断, 识图未启动.")
        refs, meta = vision_mod.refs_from_path(
            tools._resolve(self.ctx, str(path or "")), frames=frames)
        if refs is None:
            return tools.ToolRet(
                False, "look_image: 读不到 %r —— %s(检查路径: 相对路径相对当前"
                       "工作目录; 截图/用户拖进来的图都是路径)." % (path, meta))
        prov, who = self.provider, "%s/%s" % (self.provider.name, self.provider.model)
        if model:
            fn = self.ctx.sub_provider_fn
            if fn is None:
                return tools.ToolRet(
                    False, "look_image: 点名识图模型(%r) 解析不到 —— 省略 model: 当前"
                           "模型是视觉档就直接用它; 或到「模型」页给视觉条目勾「能看图」"
                           "再点名." % model)
            allow = self.ctx.sub_allow_other      # 与 spawn 同一道门(live callable)
            try:
                allow = bool(allow() if callable(allow) else allow)
            except Exception:
                allow = False
            if not allow:
                return tools.ToolRet(
                    False, "look_image: 点名别的模型须先开 设置→分身「允许用非当前模型」"
                           "(与分身同一道开关) —— 省略 model 则用当前模型(须视觉档).")
            try:
                prov2 = fn(model)
            except Exception:
                prov2 = None
            if prov2 is None:                     # 点名了却解析不到: 回喂视觉档候选自纠
                names = self._vision_candidates()
                tail = ("可用的视觉档: " + ", ".join(names[:20])) if names else \
                    "先让用户在「模型」页勾选「能看图」的条目"
                return tools.ToolRet(
                    False, "look_image: 清单里没有可用的 %r(写法=家/型号, 如 "
                           "qwen/qwen3-vl-plus) —— %s." % (model, tail))
            prov, who = prov2, model
        elif not getattr(self.provider, "vision", False):
            names = self._vision_candidates()
            tail = ("可用的视觉档: " + ", ".join(names[:20])) if names else \
                "让用户在「模型」页给条目勾上「能看图」"
            return tools.ToolRet(
                False, "look_image: 当前模型不是视觉档(没勾「能看图」), 图喂不进去 —— "
                       "① 到「模型」页给视觉条目勾「能看图」再点名 model ② 换当前模型"
                       "为视觉档. %s." % tail)
        t0 = time.time()
        looker = AgentLoop(
            prov, tools.build_registry(tools=[]), SubBusProxy(self.bus, "look", "识图"),
            sysprompt="你是识图助手: 用户给一张或几张图/视频帧和一个问题, 只依据图与"
                      "问题作答, 直接给结论(关键细节别漏, 不确定就说不确定).",
            max_iters=2, cwd=self.ctx.cwd, session=None,   # 不落 transcript, 不带工具
            perms=self.perms, knobs=self.knobs, stream=False, auto_memory=False,
            ask_user=None, platform=None, hooks=self.hooks)
        if meta:                        # GIF 动图/视频: 元数据织进问题, 模型有上下文
            question = "【媒体信息】%s\n%s" % (meta,
                question or "描述这张图的内容.")
        try:
            s = looker.run_turn(question or "描述这张图的内容.", images=refs)
        except Exception as ex:
            return tools.ToolRet(False, "look_image: 识图调用崩了: %s: %s"
                                       % (type(ex).__name__, ex))
        with self._sub_lock:                      # 账目并回本轮(同分身口径)
            for k in self._sub_usage:
                self._sub_usage[k] += (s.usage_total or {}).get(k, 0) or 0
        ok = not s.aborted and not s.error
        body = (s.final_text or "").strip()
        try:                                      # M19: 跨模型花钱留客观账(失败静默)
            import ledger as ledger_mod
            ledger_mod.append({
                "ts": t0,
                "sid": (self.session.sid if self.session is not None
                        else getattr(self.bus, "session", "")),
                "parent": "%s/%s" % (self.provider.name, self.provider.model),
                "sub": "look_image", "model": who,
                "task": (question or "")[:ledger_mod.TASK_HEAD],
                "ok": ok,
                "tokens": {"prompt_tokens": s.usage_total.get("prompt_tokens", 0),
                           "completion_tokens": s.usage_total.get("completion_tokens", 0),
                           "total_tokens": s.usage_total.get("total_tokens", 0)},
                "wall_s": round(time.time() - t0, 2),
                "result_head": body[:ledger_mod.RESULT_HEAD]})
        except Exception:
            pass
        # 头行不带墙钟(账本里有): trace 走 golden 逐字节 diff, 时间量会炸回归
        head = "[识图] 模型=%s · in %s/out %s tokens" % (
            who, s.usage_total.get("prompt_tokens", 0),
            s.usage_total.get("completion_tokens", 0))
        if s.error:
            head += " · 出错: %s" % str(s.error)[:120]
        text = head + "\n—— 识图结论 ——\n" + (body or "(没有得出结论)")
        if refs:                         # 2026-10-20: 告诉模型图已上流, 用户看得到
            text += "\n(已附 %d 张图进聊天流, 用户侧已可见)" % len(refs)
        return tools.ToolRet(ok, text[:SUB_RESULT_CAP],
                             truncated=len(text) > SUB_RESULT_CAP,
                             images=refs)   # 2026-10-20: 图随结论进聊天流

    def _gate_and_dispatch(self, name, args_raw):
        """M2 权限门: 过门才执行; 拒绝不是异常 —— 拒绝消息作为 error ToolRet 回喂模型自纠."""
        if self.perms is None:
            return self.registry.dispatch(name, args_raw, self.ctx)
        tool = self.registry.get(name) or {"name": name, "risk": "write"}
        _, parsed = tools.parse_args(args_raw)
        verdict, by = self.perms.decide(tool, parsed)
        self.bus.emit("perm_decision", name=name, decision=verdict, allowed_by=by)
        if verdict == "allow":
            # M25: pre 钩子(权限之后执行之前); deny → 与权限拒绝同一条自纠通道
            if getattr(self, "hooks", None):
                allowed, reason, metas = hooks_mod.run_pre(self.hooks, name, parsed)
                for m in metas:
                    self.bus.emit("hook_run", phase="pre", **m)
                if not allowed:
                    return tools.ToolRet(
                        False, "钩子拒绝(%s): %s —— 不要原样重试, 按说明调整或"
                               "向用户说明." % (name, reason))
            ret = self.registry.dispatch(name, args_raw, self.ctx)
            if getattr(self, "hooks", None):
                for m in hooks_mod.run_post(self.hooks, name, parsed,
                                            ret.ok, ret.content[:400]):
                    self.bus.emit("hook_run", phase="post", **m)
            return ret
        return tools.ToolRet(False, "权限拒绝(%s): 本次 %s 调用未被允许. "
                                  "不要原样重试, 换只读途径或向用户说明." % (by, name))

    def _fixup_after_interrupt(self):
        """中断后修消息列: 未配对的 tool_call 补 aborted, 收尾一条 assistant 说明."""
        last_as = None
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_as = m
                break
        if last_as is not None:
            start = self.messages.index(last_as)
            paired = {m.get("tool_call_id") for m in self.messages[start:]
                      if m.get("role") == "tool"}
            for tc in last_as["tool_calls"]:
                if tc.get("id") not in paired:
                    self._append({"role": "tool", "tool_call_id": tc["id"],
                                  "content": "aborted by user"})
        self._append({"role": "assistant", "content": "(用户中断了本轮操作)"})

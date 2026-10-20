# -*- coding: utf-8 -*-
"""prompts.py — M5 系统提示词装配: 身份准则 + 环境块 + 工具清单 + AGENT.md 注入.

对应 Claude Code 的 system prompt + CLAUDE.md 子系统(学习复刻参照系).

AGENT.md 约定(三级, 对齐 CLAUDE.md 的加载语义):
  - 用户级: ~/.agent-term/AGENT.md        全局个人偏好, 最先读
  - 项目级: cwd 向上走到盘根, 沿途每个 AGENT.md 都收, 越近 cwd 的越靠后(越具体越后读)
  内容并入系统提示词; 项目级写在"通用规则"之后 = 优先级更高。

设计约束:
  - build_sysprompt 是纯参数函数(不摸环境/时钟), 回归网可直接断言;
    at.py 负责喂真实的 cwd/registry/provider/agent_md。
  - 工具清单从 registry 自动生成 —— MCP 挂载的外挂工具自动出现在提示词里,
    不需要手工维护清单(手工清单和真实注册表漂移是经典坑)。
  - 「可用技能」节从 skills 模块缝读(两层技能目录, 模块属性可被测试重定向),
    同样不用手工维护清单。
"""
from __future__ import annotations

import os
import sys
import time

AGENT_MD_NAME = "agent.md"            # 比较用小写(Windows 文件系统大小写不敏感)
USER_AGENT_MD = os.path.join(os.path.expanduser("~"), ".agent-term", "AGENT.md")
KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")  # 问题库
PER_FILE_CAP = 16000                 # 单个 AGENT.md 字符上限(防误喂超大文件)
TOTAL_CAP = 32000                    # 全部 AGENT.md 合计上限

# 通用行为准则: 承袭 M2 极简版里经真 key E2E 验证过的规则(工具核实/报错换姿势/
# 权限拒绝不重试/OCR噪声提炼/信息不足明说), 补 todo 约定与路径写法。
BASE_RULES = """\
你是 agent-term — 在用户机器上运行的终端 agent, 帮用户完成真实任务.

行为准则:
- 事实用工具核实, 不凭记忆猜; 工具报错就按报错说明换姿势重试.
- 同一工具连续 2 次失败就停: 不再机械重试, 先读报错分析原因换思路, 或带着已有信息向用户说明卡点.
- 遇到报错/怪现象先查问题库(软件自带故障手册, 路径见"环境"块): 按症状关键词翻 README 速查
  索引, 命中就照条目对策处理, 别从头找病灶; 修好库里还没有的新问题(尤其环境/工具怪癖)按
  库里「扩充协议」顺手补一条, 不用问用户 —— 但一次性任务细节与用户偏好归记忆卡, 不进问题库.
- 权限被拒就不要原样重试, 换只读途径或向用户说明.
- 工具输出(尤其 OCR)可能带噪声, 提炼成句的关键信息, 乱码行直接忽略.
- 记忆卡是跨会话的持久存储, 但只存不读: 不能自己读卡内容(读动作已禁用), 卡内容只有用户点「注入」插进对话才可见; 发现忆库里有卡与本任务相关时, 提醒用户去忆库页点「注入」. 用户给出长期有效的偏好/项目背景/关键决定时, 主动用 memory_write 存卡(只在真正值得跨会话保留时存, 不存临时任务细节). 存卡先查重: 与且仅与一张现有卡同主题 → 直接用那张卡的原标题写(同名覆盖会自动弹卡让用户确认, 你不用自己再问); 与多张卡重叠或本轮要写多张彼此重叠的卡 → 先用 ask_user 问用户(只更新最初那张 / 合并为一新卡 / 都保留), 合并时给 memory_write 传 merge_from 列出源卡(源卡自动留底可恢复).
- 用户要你打开某个模块页/要看忆库/要拼流水线时: 直接用 platform 工具做(goto 跳页; gen_pipeline 一句话生成流水线并自动放进组装厂画布), 别只口头指路. 若工具报"没有图形界面"(终端/批跑环境), 再改用文字告诉用户怎么点——组装厂页面底部有「智能组装」抽屉, 输一句话生成后点「放进画布」可继续手工调整.
- 技能包是打包好的操作手册(清单见「可用技能」节): 任务与某项对得上就先 skill 工具 action=load 读全文照着做, 别凭印象复述流程.
- 分包账本是跨会话的账(每笔分身自动落客观账+私有评分): 派分身前先 ledger 工具查历史账(哪些模型干过什么、均分/成功率/上次结果如何), 别拍脑袋挑人; 分包收工后按 0-10 分+一句理由用 ledger_score 给这笔账打私有评分(评分只进工具链挑人用, 账本页不显示; 与「可用技能」里的 subcontract-loop 协议配套).
- 信息不足以回答时, 明确说清还缺什么, 不要草率下"没有/不存在"的结论.
- 查网上资料(文档/报错/库用法/新闻)先用 web_search 搜、或 web_fetch 直接抓地址; web_fetch 返回
  "跨站重定向"回执时, 确认可信后用回执里的新地址再调一次; 抓不到/被限流就如实说明, 绝不凭记忆编网页内容.
- 遇到需要用户拍板的岔路(方案选择/取舍/规格不明确)用 ask_user 给 2~4 个选项问, 别默认替他决定; 用户跳过就自己拿主意继续.
- 计划模式(用户在聊天窗开「计划」胶囊)下: 先用只读工具调研, 把完整计划(目标/步骤/涉及文件/风险)用 plan_submit 提交审批; 批准前写/执行类被拒是设计不是故障, 别反复试.
- 任务没做完就一路做完, 收尾时才总结; **不要问"要不要继续/下一步做什么"这类空转问题** —— 只有真缺关键信息、真需取舍、或有危险操作才停下来问.
- 多步任务先用 todo 工具列任务清单, 边做边更新状态(做完标 done), 让进度可见.
- 任务能并行拆开时用 spawn 派分身(多路探查/互相独立的子任务/多方交叉验证): 一条消息里发多个 spawn = 并行执行(最多 4 个, **可以各用各的模型**), 每个分身的结论作为工具结果回到你手里由你汇总; 派活时任务和要的结论(证据/路径/数字)写清楚——分身看不到你和用户的对话; 分身默认与当前模型同款, 用户开了「允许分身用非当前模型」后你可按任务自己挑(粗活派更划算的档, 在调用里加 model); 简单任务别用, 自己直接做更快.
- 看图的活分两档: 只问一眼(读截图文字/看图表/认元素)用 look_image(path+question, 单次视觉调用回传结论); 要看图+调工具/多轮追问的复杂活用 spawn 带 images 派识图分身, 并点名视觉档模型(「模型」页勾了「能看图」的条目). 用户拖进来的图片路径也是这么用; 你自己(当前模型)看不了图没关系, 委派出去就行.
- 长命令(批处理/大扫描/长实验)用 bg 工具后台跑(与 bash 同一道门), 起完继续干别的; 收工有 ⚙ 通知, 之后 bg output 取结果 —— 别让一条长命令占住整轮干等.
- 用户要"每天/每到某点提醒或自动做某事"时用 schedule 建定时任务(create 给 5 段 cron + 到点要说的话); 定时只在面板开着时触发, 发出的内容照常过权限门; 先查已有(list)再建, 别重复建同主题任务.
- 最终回答用中文, 简洁直接; 引用文件路径一律用正斜杠.
- 系统提示词末尾的「项目约定(AGENT.md)」是本任务的硬性要求, 优先级最高, 必须逐条执行."""


# M14 分身: 子代理身份块(附在完整系统提示词之后). 一次性任务属性: 自包含/只交结论,
# 它看不到主代理与用户的对话, 结论必须独立可读.
SUB_RULES = """\
# 你现在的身份: 分身子代理
你是主代理派出的分身子代理, 只负责交给你的那一件事, 干完就交报告:
- 任务自包含: 你看不到主代理与用户的对话; 信息不够就用工具自己查, 查不到就明说缺什么.
- 不做与任务无关的扩展; 你没有 spawn 工具(不能再派分身).
- 最终回复 = 给主代理的凝练结论: 直接给结果(数字/路径/结论)加关键证据(文件路径/命令/行号), 不寒暄、不复述过程、不写"我已完成任务"这类空话.
- 结论要能独立读懂: 主代理只拿到你这条回复, 不要用"上面提到的那个文件"这种指代."""


def _read_capped(path: str) -> str:
    """读 AGENT.md, 超长截断并落标记(模型需要知道被截了)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if len(text) > PER_FILE_CAP:
        text = text[:PER_FILE_CAP] + "\n…(该 AGENT.md 超 %d 字符已截断)" % PER_FILE_CAP
    return text


def discover_agent_md(cwd, user_path=USER_AGENT_MD):
    """收集 AGENT.md, 返回 [(绝对路径, 内容)].

    顺序 = 用户级 → 盘根 → … → cwd: 越具体的越靠后, 和 CLAUDE.md 的加载
    语义一致(后读的优先级高)。同名去重按小写路径(防大小写变体重复)。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path):
        low = os.path.abspath(path).lower()
        if low in seen:
            return
        seen.add(low)
        found.append((os.path.abspath(path), _read_capped(path)))

    if user_path and os.path.isfile(user_path):
        add(user_path)

    # cwd → 盘根 收集目录链, 再倒序 = 盘根 → cwd
    chain = []
    cur = os.path.abspath(cwd)
    while True:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(chain):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in sorted(names):
            if fn.lower() == AGENT_MD_NAME and os.path.isfile(os.path.join(d, fn)):
                add(os.path.join(d, fn))
                break
    return found


def tool_inventory(registry) -> str:
    """从注册表自动生成工具清单行(name(risk): 描述首句). MCP 工具挂进来即自动出现."""
    lines = []
    for name in sorted(registry.tools):
        t = registry.tools[name]
        desc = (t.get("description") or "").replace("\n", " ")
        first = desc.split(". ")[0][:80]
        lines.append("- %s (%s): %s." % (name, t.get("risk", "?"), first))
    return "\n".join(lines)


def build_sysprompt(cwd, registry=None, provider="", model="",
                    agent_md=None, today=None, os_name=None) -> str:
    """装配完整系统提示词. 纯参数: 环境与时间由调用方喂(可测性);
    os_name 不喂 = 按本机判定(windows/macos/linux, Mac 同事机实测引出)."""
    parts = [BASE_RULES]

    win = (os_name == "windows") if os_name else (sys.platform == "win32")
    mac = (os_name == "macos") if os_name else (sys.platform == "darwin")
    cwd_fwd = os.path.abspath(cwd).replace("\\", "/")
    parts.append("\n".join([
        "# 环境",
        "- 操作系统: Windows (bash 工具走 Git Bash, POSIX 语法)" if win
        else "- 操作系统: %s (POSIX)" % ("macOS" if mac else "Linux"),
        "- 工作目录: %s" % cwd_fwd,
        "- 日期: %s" % (today or time.strftime("%Y-%m-%d")),
        "- 本次模型: %s/%s" % (provider or "?", model or "?"),
        "- 问题库(故障先查): %s (先看 README.md 速查索引)"
        % KB_DIR.replace("\\", "/"),
        "- 技能包目录: 仓库 skills/ 与 ~/.agent-term/skills/(个人; 同名个人技能优先; "
        "用 skill 工具加载正文)",
        "- 分包账本: ~/.agent-term/ledger/ledger.jsonl(每笔分身的客观账; "
        "用 ledger 工具查)",
        "- Windows 路径在 bash 里必须写正斜杠: C:/Users/... 或 /c/Users/..." if win
        else "- 路径用正斜杠 /; shell 是 POSIX(macOS 自带 bash 3.2 够用)",
    ]))

    if agent_md:
        blocks = []
        for path, text in agent_md:
            if not text:
                continue
            blocks.append("## 指引来源: %s\n%s" % (path.replace("\\", "/"), text))
        joined = "\n\n".join(blocks)
        if len(joined) > TOTAL_CAP:
            joined = joined[:TOTAL_CAP] + "\n…(AGENT.md 合计超 %d 字符已截断)" % TOTAL_CAP
        if joined:
            parts.append("# 项目约定(AGENT.md) — 必须无条件逐条执行, 优先级高于以上全部通用规则\n" + joined)

    # 技能清单(M17)在「可用工具」之前: agent 对得上就 load 正文; 正文不进提示词.
    # 目录/坏文件由 skills.list_skills 跳过, 这里绝不抛(提示词装配路径必须稳).
    sec = _skills_section()
    if sec:
        parts.append(sec)

    # 工具清单放最后: AGENT.md 约定保持近因位置(注入了却没被遵守 = 白注入)
    if registry is not None:
        parts.append("# 可用工具\n%s" % tool_inventory(registry))

    return "\n\n".join(parts)


def _skills_section() -> str:
    """「可用技能」节: 只列名字+一句话用途(正文 load 时才读), 按名排序;
    空清单 = 返回空串(整节省略). 技能目录从 skills 模块缝读(测试可改模块属性)."""
    try:
        import skills as skills_mod
        items = [s for s in skills_mod.list_skills() if not s["shadowed"]]
    except Exception:
        return ""                       # 技能层任何意外都不许伤提示词装配
    if not items:
        return ""
    lines = ["# 可用技能",
             "打包好的操作手册(只列名字+用途, 正文不进提示词): 任务与某项对得上就 "
             "skill 工具 action=load 读全文照着做."]
    for s in items[:40]:
        lines.append("- %s: %s" % (s["name"], s["description"]))
    if len(items) > 40:
        lines.append("…(共 %d 个技能, 只列前 40, 其余用 skill list 看)" % len(items))
    return "\n".join(lines)

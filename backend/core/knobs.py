# -*- coding: utf-8 -*-
"""knobs.py — M6 实验旋钮层: CLI/env/REPL → payload 注入 → trace 标注.

研究仪器定位的核心件(对应 Claude Code 的 model config 子系统, 但目的不同:
CC 的旋钮是产品功能, 这里的旋钮是**实验自变量** —— 所以设计原则反过来:
  1. 映射表数据驱动: 家差异(同名旋钮各家字段名/取值不同)全在 KNOB_MAP,
     验证出新映射 = 改一行, 不动代码逻辑。
  2. 不猜不拦: 仪器不替模型家做主 —— 未映射的 (knob, family) 组合不注入,
     record 里写明 unmapped; 注入后上游报错(400/参数不支持)由 provider 层
     原样带回, trace 里有发出去的完整 payload, 复盘时证据链完整。
  3. 发了什么记什么: apply() 返回 record(每个旋钮的值/落点/或没落的原因),
     agent 层把它并进 llm_request 事件 —— 研究扫描不用重解 payload。

来源与优先级: 显式 dict(REPL /knob) > CLI 便捷 flag(--effort 等) >
AT_KNOBS env("k=v,k=v") —— 同名后者被前者覆盖(字典 merge 顺序)。
"""
from __future__ import annotations

import json

# ------ 已知旋钮与家映射 ------
# 直通旋钮: OpenAI 兼容协议的顶层标准字段, 所有家都直接放 payload 顶层。
DIRECT_KNOBS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    # 未来: frequency_penalty / presence_penalty / seed ...
}

# effort(思考力度)的家差异映射: 家(小写) → payload 落点。
# 取值透传不校验(low/high/max/medium...各家词表不同, 上游说了算)。
# 未列出的家 = unmapped(不注入), 验证通了一家加一行。
# 键一律小写: providers.json 的 family 是 'DeepSeek'/'GLM' 这类混合大小写,
# apply() 侧统一 lower 再查表(实测踩过: 'GLM' ≠ 'glm' 静默漏配)。
# 实测依据(2026-10 前后): deepseek v3.1+/v4 原生 reasoning_effort;
# glm 5系 thinking 参数不可关(强关 400), 映射待实测再上。
EFFORT_MAP = {
    "deepseek": {"field": "reasoning_effort", "style": "value"},
}

# 仅供展示/帮助文本, 解析不依赖它(未知旋钮也收, 走 generic 直通)。
KNOWN_KNOBS = sorted(set(DIRECT_KNOBS) | {"effort"})


def parse_value(raw: str):
    """旋钮值解析: JSON 优先(支持 true/3.3/{"type":"enabled"} 这类结构),
    解析失败按原字符串回退 —— 宁可透传原样, 不替用户改类型。"""
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        return raw


def parse_pairs(pairs) -> dict:
    """["effort=low", "thinking={\"type\":\"enabled\"}"] → {effort:"low", thinking:{...}}.
    坏形状(无=)跳过不崩 —— 旋钮是实验件, 输入错不该挡住主流程。"""
    out = {}
    for item in pairs or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        k, _, v = item.partition("=")
        k = k.strip()
        if k:
            out[k] = parse_value(v)
    return out


def parse_env(text: str) -> dict:
    """AT_KNOBS="effort=low,temperature=0.3" → dict."""
    if not text or not text.strip():
        return {}
    return parse_pairs([seg for seg in text.split(",") if seg.strip()])


def merge(*sources) -> dict:
    """按序 merge, 后者覆盖前者(显式 > CLI > env 的实现处)。"""
    out = {}
    for src in sources:
        if src:
            out.update(src)
    return out


def apply(payload: dict, knobs: dict | None, family: str):
    """把旋钮注入 payload. 返回 (新 payload, record).

    record: {knob名: {"value": 原值, "applied": bool, "field": 落点或None,
                       "note": unmapped/direct/... 一句说明}} —— 进 trace,
    研究侧按 (family, knob) 分组即得自变量标注。payload 本身不动原 dict。"""
    record = {}
    if not knobs:
        return payload, record
    out = dict(payload)
    for key, value in knobs.items():
        if key in DIRECT_KNOBS:
            field = DIRECT_KNOBS[key]
            out[field] = value
            record[key] = {"value": value, "applied": True,
                           "field": field, "note": "direct"}
        elif key == "effort":
            spec = EFFORT_MAP.get((family or "").lower())
            if spec:
                field = spec["field"]
                if spec.get("style") == "value":
                    out[field] = value
                else:
                    out[field] = value   # 其他 style 未来再展开, 现只有 value
                record[key] = {"value": value, "applied": True,
                               "field": field, "note": "family-map"}
            else:
                record[key] = {"value": value, "applied": False, "field": None,
                               "note": "unmapped: family=%r 无 effort 映射(不猜)" % family}
        else:
            # generic 直通: 任意未来旋钮直接放 payload 顶层(实验自由度兜底)。
            # 已知撞名 DIRECT_KNOBS/effort 的走上面分支, 到这的都是新名字。
            out[key] = value
            record[key] = {"value": value, "applied": True,
                           "field": key, "note": "generic-direct"}
    return out, record

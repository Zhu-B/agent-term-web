# -*- coding: utf-8 -*-
r"""provider.py — LLM provider 层: 读 providers.json, urllib 非流式调用, 抹平家差异.

家法(与作者其它工具 stock/gant 系一脉相承):
  - key 只读环境变量(providers.json 每家的 key_env 字段), 绝不落盘;
    无 key = available=False 不崩, 由入口层给指引.
  - urllib.request 直连 OpenAI 兼容 /chat/completions, 非流式, 120s 超时,
    失败返回 None 并留 last_error(调用方决定重试/降级).
  - 所有 open() 显式 encoding="utf-8".

关键设计: build_payload 与 send 分成两截 —— agent 拿到 payload 先落 trace 再发送,
中间这道缝就是 M6 实验旋钮(temperature/effort/...)的注入点, 发了什么 trace 里全有.

内部消息规范形 = OpenAI chat 格式(规范形=wire 形, 免来回转换丢信息;
Anthropic 适配器是 M6 的活, 到时在本层做双向转换).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

# ------ Config ------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_CHAIN = [
    os.environ.get("AT_PROVIDERS"),                       # 显式覆盖
    os.path.join(BASE_DIR, "providers.json"),             # 仓库本地配置(gitignore, 见 example)
    os.path.join(os.path.expanduser("~"), "providers.json"),  # 用户主目录共享配置
    os.path.join(BASE_DIR, "providers.example.json"),     # 最后一级兜底 = 开箱即用(mcpclient 同款)
]
TIMEOUT = 120

# ------ 本地 key 库(M8 前端导入功能引入; 家法修订并收窄; M-k2 多槽) ------
# 原文"key 只读环境变量绝不落盘"收窄为: key 绝不进 仓库/trace/事件流/网络上传.
# GUI 导入的 key 存用户主目录 ~/.agent-term/keys.json(仓库外);
# 优先级 = 环境变量 > 本地库首槽(环境变量在场时本地库同名 key 不生效).
#
# 文件形状(值两代兼容):
#   {"glm": [{"label": "公司", "key": "sk-…"}, {"label": "个人", "key": "sk-…"}]}   多槽(新)
#   {"deepseek": "sk-…"}                                                            单槽(旧)
# 双视图: load_key_store() 保持旧形状({name: key字符串}, = 首槽), 旧读者
# (voice.py/load_providers/回归网)零改动; load_key_slots() 给全量槽列表,
# 模型页多 key 管理(GLM 公司/个人各一个会话入口)吃这一份.
KEY_STORE = os.path.join(os.path.expanduser("~"), ".agent-term", "keys.json")


def _norm_slots(v) -> list:
    """原始值 → 槽列表. 旧字符串 → 单无 label 槽; list 逐项清洗(空 key 丢弃)."""
    if isinstance(v, str):
        return [{"label": "", "key": v.strip()}] if v.strip() else []
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        if isinstance(item, str):
            if item.strip():
                out.append({"label": "", "key": item.strip()})
        elif isinstance(item, dict):
            k = (item.get("key") or "").strip()
            if k:
                out.append({"label": (item.get("label") or "").strip(), "key": k})
    return out


def load_key_slots() -> dict:
    """全量槽视图: {name: [{"label","key"}, …]}(旧形状归一, 空家/坏值丢弃)."""
    try:
        with open(KEY_STORE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, v in data.items():
        slots = _norm_slots(v)
        if slots:
            out[name] = slots
    return out


def load_key_store() -> dict:
    """旧形状视图(首槽): {name: key字符串}. 多槽家取首槽(=该家默认 key)."""
    return {name: slots[0]["key"] for name, slots in load_key_slots().items()}


def _write_slots(all_slots: dict) -> None:
    """整份落盘. 单无 label 槽写回旧字符串形状(文件素净, 手工编辑友好)."""
    os.makedirs(os.path.dirname(KEY_STORE), exist_ok=True)
    data = {}
    for name, slots in all_slots.items():
        if len(slots) == 1 and not slots[0]["label"]:
            data[name] = slots[0]["key"]
        else:
            data[name] = slots
    with open(KEY_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_slots(name: str, slots: list) -> None:
    """整家替换为给定槽列表(空列表=删家条目). 一般走 add_slot/delete_slot."""
    all_slots = load_key_slots()
    clean = _norm_slots(slots)
    if clean:
        all_slots[name] = clean
    else:
        all_slots.pop(name, None)
    _write_slots(all_slots)


def save_key(name: str, key: str) -> str:
    """旧接口(语义保留): 整家替换为单无 label 槽; 空 key = 删除该家条目."""
    save_slots(name, [{"label": "", "key": key or ""}])
    return KEY_STORE


def delete_key(name: str) -> None:
    save_key(name, "")


def first_key(name: str) -> str:
    """该家首槽 key(= 本地库视角的默认 key); 无则 ""."""
    slots = load_key_slots().get(name) or []
    return slots[0]["key"] if slots else ""


def slot_key(name: str, label: str) -> str:
    """按槽名查 key(槽不存在返 "")."""
    for s in load_key_slots().get(name) or []:
        if s["label"] == label:
            return s["key"]
    return ""


def add_slot(name: str, label: str, key: str) -> None:
    """给家加一把命名 key(追加末位). label 必填且同家唯一 → 违规抛 ValueError(UI 转人话)."""
    label, key = (label or "").strip(), (key or "").strip()
    if not label:
        raise ValueError("key 名称必填(如 公司/个人)")
    if not key:
        raise ValueError("key 不能为空")
    slots = load_key_slots().get(name) or []
    if any(s["label"] == label for s in slots):
        raise ValueError("名称「%s」这家已用过, 换一个" % label)
    dup = next((s for s in slots if s["key"] == key), None)
    if dup is not None:
        # 防双粘: 文案不回显 key 明文
        raise ValueError("这把 key 本家已经有了(槽「%s」)" % (dup["label"] or "未命名"))
    slots.append({"label": label, "key": key})
    save_slots(name, slots)


def delete_slot(name: str, label: str) -> None:
    """删一个槽(删空即删家条目)."""
    slots = [s for s in load_key_slots().get(name) or [] if s["label"] != label]
    save_slots(name, slots)


def promote_slot(name: str, label: str) -> None:
    """挪到首位 = 该家默认槽(env 不在场时对 CLI/家默认档生效)."""
    slots = load_key_slots().get(name) or []
    idx = next((i for i, s in enumerate(slots) if s["label"] == label), None)
    if idx is None or idx == 0:
        return
    slots.insert(0, slots.pop(idx))
    save_slots(name, slots)


def mask_key(key: str) -> str:
    """key 显示用掩码(首6尾4; 短 key 全掩) —— UI/自检共用, 防明文上屏/入档."""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) > 12:
        return "%s…%s" % (k[:6], k[-4:])
    return "•" * min(len(k), 8)


# ------ M15 发送前 key 安检: 疑似 API key 探测(宁可多问) ------
_KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-\.]{10,}"),                # OpenAI/DeepSeek/硅基 系
    re.compile(r"\b[0-9a-fA-F]{16,}\.[A-Za-z0-9]{8,}\b"),   # 智谱 id.secret 形
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),                    # 裸长十六进制
    re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9_\-])"),  # 超长令牌
)


def looks_like_key(text: str) -> list:
    """扫文本里疑似 API key 的片段 → 片段列表(去重 ≤3; 展示用 mask_key).
    判定: sk- 前缀必中; 其余要字母+数字混合(纯词/纯数字不算); 已中片段包含的
    子串不再重复报(如 sk- 串里嵌的 32hex). 误报由用户一句话放行,
    真 key 靠发送前的两道确认拦(见 frontend._key_gate)."""
    hits = []
    for rx in _KEY_PATTERNS:
        for m in rx.finditer(text or ""):
            s = m.group(0)
            if not s.startswith("sk-") and not (any(c.isdigit() for c in s)
                                                and any(c.isalpha() for c in s)):
                continue
            low = s.lower()
            if any(low in h.lower() or h.lower() in low for h in hits):
                continue                       # 与已中片段互相包含 = 同一把, 不重复报
            hits.append(s)
            if len(hits) >= 3:
                return hits
    return hits


def slot_refs(name: str, label: str) -> list:
    """引用该槽的模型条目(删槽前的"谁在用"提示用; models.json 里 key_slot==label)."""
    return [m for m in load_models()
            if m.get("provider") == name and m.get("key_slot") == label]


def refresh_provider_key(p, store=None) -> None:
    """家级默认 key 的唯一权威合成: env 优先 > 首槽 > 无(家法口径不动).

    同步 p.key / p.available / p.key_from_store / p.key_slot(生效槽 label).
    槽增删/置顶后 UI 也走这条, 防 p.key 陈旧."""
    env = os.environ.get(p.key_env) if p.key_env else None
    if env:
        p.key, p.available, p.key_from_store, p.key_slot = env, True, False, ""
        return
    slots = (store if store is not None else load_key_slots()).get(p.name) or []
    if slots:
        p.key, p.available = slots[0]["key"], True
        p.key_from_store, p.key_slot = True, slots[0]["label"]
    else:
        p.key, p.available, p.key_from_store, p.key_slot = None, False, False, ""


def key_source(p) -> str:
    """该 provider 当前 key 来源: "env" | "store" | ""(未配置)."""
    if p.key_env and os.environ.get(p.key_env):
        return "env"
    return "store" if getattr(p, "key_from_store", False) else ""


# ------ 模型族推断(通道≠族: qwen 通道也托管 deepseek/kimi 等异族模型) ------
# 表驱动, 顺序敏感(短的宽词放后). 匹配 = id 小写按 '/' 分段, 段首命中
# (vendor/model 形如 "Qwen/Qwen2.5-7B-Instruct" 与裸名 "deepseek-v3" 两吃);
# 段首都不中再看整串包含. 认不出返 ""(归"其它") —— 纯函数, UI/研究口径共用.
# 规范化族名用手熟的产品名(Kimi/Llama/Gemma…), 与家 family 字段两侧都过
# 本函数归一后比较, 内部自洽即可; "OpenAI-compat"/"按模型定"这类家值也自然落位.
FAMILY_HINTS = (
    ("deepseek", "DeepSeek"), ("qwq", "Qwen"), ("qwen", "Qwen"),
    ("chatglm", "GLM"), ("glm", "GLM"), ("zhipu", "GLM"),
    ("moonshot", "Kimi"), ("kimi", "Kimi"),
    ("codellama", "Llama"), ("llama", "Llama"),
    ("codestral", "Mistral"), ("mixtral", "Mistral"), ("mistral", "Mistral"),
    ("gemma", "Gemma"), ("gemini", "Gemini"),
    ("openai", "OpenAI"), ("gpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"),
    ("anthropic", "Anthropic"), ("claude", "Anthropic"),
    ("command-r", "Cohere"), ("phi-", "Phi"), ("grok", "Grok"),
    ("yi-", "Yi"), ("baichuan", "Baichuan"), ("abab", "MiniMax"),
    ("minimax", "MiniMax"), ("stepfun", "Step"), ("step-", "Step"),
    ("hunyuan", "Hunyuan"), ("doubao", "Doubao"), ("ernie", "Ernie"),
    ("internlm", "InternLM"),
)


def model_family(mid: str) -> str:
    """按型号名推断模型族(通道≠族); 认不出返 "". 纯函数, 不改任何状态."""
    s = (mid or "").lower()
    for seg in s.split("/"):
        for pref, fam in FAMILY_HINTS:
            if seg.startswith(pref):
                return fam
    for pref, fam in FAMILY_HINTS:
        if pref in s:
            return fam
    return ""


# ------ 本地模型库(M8.1 模型页: 用户自增的模型档位) ------
# 两类条目(都在 ~/.agent-term/models.json, 仓库外):
#   ① 家内档位: {"provider": 家名(providers.json 里的), "model": 型号}
#      —— 连接配置(endpoint/key_env)仍以 providers.json 为准, 不复制.
#   ② 中转站:   {"provider": "custom", "label": 名称, "base_url": "https://…/v1",
#                "key": "sk-…", "model": 型号, "family": "OpenAI-compat"(可选)}
#      —— 自带连接配置与 key(OpenAI 兼容协议的中转都走这个).
MODELS_STORE = os.path.join(os.path.expanduser("~"), ".agent-term", "models.json")

# ------ 用户偏好(M8.4: 默认家/档) ------
# "设为默认"写这里 —— providers.json 是共享配置(课题实验也在消费), 本程序不自动改写它.
SETTINGS_STORE = os.path.join(os.path.expanduser("~"), ".agent-term", "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_STORE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_default_model(provider: str, model: str) -> None:
    """把 (家, 型号) 设为用户默认: 家成为优先选家, 型号为该家默认档."""
    s = load_settings()
    s["preferred_provider"] = provider
    s.setdefault("defaults", {})[provider] = model
    save_settings(s)


def save_settings(data: dict) -> None:
    """整份 settings.json 落盘(M8.4 起设置项渐多, 统一出口; 只写仓库外)."""
    os.makedirs(os.path.dirname(SETTINGS_STORE), exist_ok=True)
    with open(SETTINGS_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_models() -> list:
    try:
        with open(MODELS_STORE, encoding="utf-8") as f:
            data = json.load(f)
        return [m for m in data.get("models", [])
                if isinstance(m, dict) and m.get("provider") and m.get("model")]
    except Exception:
        return []


def save_models(models: list) -> None:
    os.makedirs(os.path.dirname(MODELS_STORE), exist_ok=True)
    with open(MODELS_STORE, "w", encoding="utf-8") as f:
        json.dump({"models": models}, f, ensure_ascii=False, indent=2)


def add_model(provider: str, model: str, **extra) -> None:
    model = model.strip()
    entry = {"provider": provider, "model": model}
    entry.update({k: v for k, v in extra.items() if v})
    models = load_models()
    if model and entry not in models:
        models.append(entry)
        save_models(models)


def delete_model(provider: str, model: str, label: str = "") -> None:
    def keep(m):
        if m["provider"] != provider or m["model"] != model:
            return True
        if label and m.get("label") != label:
            return True     # 同 (custom, model) 多条时只删指定 label 的
        return False
    save_models([m for m in load_models() if keep(m)])


def custom_providers() -> dict:
    """把模型库里的中转站条目物化成 Provider(label 为家名), 与官方家同构."""
    out = {}
    for m in load_models():
        if m.get("provider") == "custom" and m.get("base_url"):
            name = m.get("label") or ("中转-" + m["model"])
            p = Provider(name, {"base_url": m["base_url"], "model": m["model"],
                                "family": m.get("family") or "OpenAI-compat",
                                "key_env": ""})
            if m.get("key"):
                p.key, p.available = m["key"], True
                p.key_from_store = True   # 口径: 非环境变量来源都算"本地"
            p.vision = bool(m.get("vision"))       # M15: 条目能力位透传
            p.imagegen = bool(m.get("imagegen"))
            out[name] = p
    return out


def load_providers() -> dict:
    """按候选链找 providers.json(家法: 链式取值), 读成 {name: Provider};
    env key 之外, 再用本地 key 库补齐缺 key 的家(GUI 导入的 key 生效处).

    链走到最后一级 = 内置示例兜底 —— 没有任何本地配置的新下载也能起
    (零 key 进模型页导入即可; 要定制端点/默认档再把示例复制成 providers.json)."""
    for path in PROVIDERS_CHAIN:
        if path and os.path.exists(path):
            if os.path.basename(path) == "providers.example.json":
                print("[provider] 无本地 providers.json, 按内置示例默认运行(要定制就复制 "
                      "providers.example.json 为 providers.json; key 走环境变量或模型页导入)",
                      flush=True)
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            providers = {name: Provider(name, item) for name, item in cfg.items()}
            store = load_key_slots()               # 只读一次, 传给每家
            for p in providers.values():
                refresh_provider_key(p, store)     # env 优先 > 首槽(家法口径不动)
            providers.update(custom_providers())   # 中转站条目一并成为可选家
            return providers
    return {}


class ChatResult:
    """一次调用的规范形结果 —— 所有 provider 家差异在此抹平."""

    def __init__(self, message, usage, finish_reason, latency_ms, raw,
                 reasoning=None, cached=False):
        self.message = message          # 规范形 assistant 消息(可能带 tool_calls)
        self.usage = usage              # {"prompt_tokens","completion_tokens","total_tokens"}
        self.finish_reason = finish_reason  # "stop" | "tool_calls" | "length" | ...
        self.latency_ms = latency_ms
        self.raw = raw                  # 原始响应 JSON 整体(插桩证据, 走 raw_sink 落盘)
        self.reasoning = reasoning      # DeepSeek-R1 系剥出来的 reasoning_content
        self.cached = cached            # AT_CACHE 回放命中(usage 记账单列, 不混算)


# 连接路线: host → "direct" | "proxy". 实测坑(2026-10-09 面板首用翻车):
# Windows 的 urllib 默认读注册表系统代理, 本地代理客户端挂着时会把
# open.bigmodel.cn 这类国内端点也打进代理 → SSL UNEXPECTED_EOF.
# 策略: 直连优先, 网络层失败(SSL/URLError/超时)才回退系统代理, 按 host 记忆
# —— 国内端点直连秒过, openrouter 等外国端点直连必败自然落代理.
_ROUTE: dict = {}


def _open(req, timeout):
    proxies = urllib.request.getproxies()
    host = req.host
    route = _ROUTE.get(host)
    plans = [True, False] if route == "proxy" else [False, True]   # 记忆只定顺序不锁死
    last = None
    for use_proxy in plans:
        handler = urllib.request.ProxyHandler(proxies if use_proxy else {})
        opener = urllib.request.build_opener(handler)
        try:
            r = opener.open(req, timeout=timeout)
            _ROUTE[host] = "proxy" if use_proxy else "direct"
            return r
        except urllib.error.HTTPError:
            raise                            # HTTP 层错误=网络已通, 语义交给上层
        except Exception as ex:              # 网络层失败 → 换路线
            last = ex
    raise last


class Provider:
    def __init__(self, name, item):
        self.name = name
        self.base_url = (item.get("base_url") or "").rstrip("/")
        self.model = item.get("model") or ""
        self.family = item.get("family") or ""
        self.price_in = item.get("price_in")
        self.price_out = item.get("price_out")
        self.key_env = item.get("key_env") or ""
        self.key = os.environ.get(self.key_env) or None if self.key_env else None
        self.available = self.key is not None
        self.key_from_store = False   # key 来自本地 key 库(GUI 导入)时为 True
        self.key_slot = ""            # 生效槽 label(本地库来源时; env 来源为空) — 展示用
        self.vision = False           # M15: 能看图(models.json 条目勾选; 回填缝按它附图)
        self.imagegen = False         # M15: 能画图(image_gen 工具的解析目标)
        self.last_error = ""

    # ------ Payload seam ------
    def build_payload(self, messages, tools=None, **extra):
        payload = {"model": self.model, "messages": messages}
        if tools:
            # 空 tools 数组整键不发 —— 有的端点对 [] 直接 400
            payload["tools"] = tools
        payload.update(extra)   # M6 旋钮从这里进, trace 全量可见
        return payload

    def send(self, payload, on_chunk=None):
        """POST /chat/completions. 失败返回 None(家法), 细节在 self.last_error.

        M6: on_chunk 不为 None → SSE 流式 —— content/reasoning 增量边收边回调
        (kind, text), 返回值仍是组装好的规范形 ChatResult(与非流式同构,
        trace/记账/回放口径全部不变)."""
        if on_chunk is not None:
            return self._send_stream(payload, on_chunk)
        url = self.base_url + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        t0 = time.time()
        try:
            with _open(req, TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as ex:
            try:
                detail = ex.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            self.last_error = "HTTP %s: %s" % (ex.code, detail)
            return None
        except Exception as ex:
            self.last_error = "%s: %s" % (type(ex).__name__, ex)
            return None
        try:
            return self._parse(data, int((time.time() - t0) * 1000))
        except Exception as ex:
            self.last_error = "bad response shape: %s: %s" % (type(ex).__name__, ex)
            return None

    # ------ Streaming (M6 收官: SSE 流式) ------
    def _stream_request(self, url, headers, wire):
        """发流式请求. 个别兼容端点不认 stream_options → 400 时摘掉该键重发一次
        (代价=末块没 usage, 记账变 0; 不猜别的形状)."""
        body = json.dumps(wire, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            return _open(req, TIMEOUT)
        except urllib.error.HTTPError as ex:
            if ex.code == 400:
                try:
                    detail = ex.read().decode("utf-8", "replace")[:300]
                except Exception:
                    detail = ""
                if "stream_options" in detail:
                    wire = {k: v for k, v in wire.items() if k != "stream_options"}
                    body = json.dumps(wire, ensure_ascii=False).encode("utf-8")
                    req = urllib.request.Request(url, data=body, method="POST",
                                                 headers=headers)
                    return _open(req, TIMEOUT)
            raise

    def _send_stream(self, payload, on_chunk):
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        # wire 形多两键(拷贝, 不动调用方 payload —— sha1 与回放口径稳定):
        # stream 必置; stream_options 尽量把 usage 要到末块(记账不断流).
        wire = dict(payload)
        wire["stream"] = True
        wire["stream_options"] = {"include_usage": True}
        t0 = time.time()
        try:
            with self._stream_request(url, headers, wire) as r:
                raw = self._consume_sse(r, on_chunk)
        except urllib.error.HTTPError as ex:
            try:
                detail = ex.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            self.last_error = "HTTP %s: %s" % (ex.code, detail)
            return None
        except Exception as ex:
            self.last_error = "%s: %s" % (type(ex).__name__, ex)
            return None
        try:
            return self._parse(raw, int((time.time() - t0) * 1000))
        except Exception as ex:
            self.last_error = "bad stream shape: %s: %s" % (type(ex).__name__, ex)
            return None

    @staticmethod
    def _consume_sse(lines, on_chunk):
        """SSE 行流(OpenAI 兼容 delta 形) → 组装成与非流式同形的 pseudo-raw
        (再走 _parse 复用全部抹平逻辑). 纯函数不碰网 —— 回归网直接喂合成行.

        on_chunk(kind, text) 只回吐 content/reasoning 增量; tool_call 分片在此
        静默组装(index 归位, id 首块到, arguments 跨块拼接), 组装完随规范形走."""
        content, reasoning = [], []
        calls = {}                      # index → {"id","name","args":[...]}
        finish, usage = "", None
        for raw_line in lines:
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", "replace")
            line = raw_line.rstrip("\r\n")
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue                # 空行 / keep-alive 注释 / 事件头, 全跳过
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]  # 末块常带 usage(choices 为空)
            ch = (chunk.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}
            for key, bucket, kind in (("content", content, "content"),
                                      ("reasoning_content", reasoning, "reasoning")):
                piece = delta.get(key)
                if piece:
                    bucket.append(piece)
                    on_chunk(kind, piece)
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0),
                                        {"id": "", "name": "", "args": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])
        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            # 借道 _parse 的剥离逻辑进 ChatResult.reasoning(规范形里本来就不带)
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [
                {"id": slot["id"], "type": "function",
                 "function": {"name": slot["name"], "arguments": "".join(slot["args"])}}
                for _, slot in sorted(calls.items())]
        return {"choices": [{"message": msg, "finish_reason": finish}],
                "usage": usage or {}, "streamed": True}

    # ------ Normalize ------
    def _parse(self, data, latency_ms):
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("empty choices")
        choice = choices[0]
        msg, reasoning = _normalize(choice.get("message") or {})
        return ChatResult(message=msg,
                          usage=data.get("usage") or {},
                          finish_reason=choice.get("finish_reason") or "",
                          latency_ms=latency_ms,
                          raw=data,
                          reasoning=reasoning)


def _normalize(msg):
    """wire 形 assistant 消息 → 规范形. 返回 (message, reasoning).

    已知家差异(计划风险清单 #3 的坑位):
      - tool_calls[].function.arguments 是 JSON *字符串*, 可能截断/坏 JSON
        → 这里不解析, 原样保留(宽容解析放 tools.dispatch, 坏 JSON 变 error ToolRet
          回喂模型自纠, 不崩).
      - glm-4-flash 偶发 tool_call 不带 id → 自造 call_<ts>_<i>, assistant 消息与
        后续 tool 回填消息两侧都用它(不修这个, 下一轮 400).
      - DeepSeek-R1 系多 reasoning_content → 剥离出消息(官方要求不要回传), 单独给
        ChatResult.reasoning, trace 的 raw 里仍有原件.
      - content 为 None(纯 tool_call 轮) → 置 "".
    """
    reasoning = msg.get("reasoning_content") or None
    out = {"role": "assistant", "content": msg.get("content") or ""}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        calls.append({
            "id": tc.get("id") or "call_%d_%d" % (int(time.time() * 1000) % 1000000, i),
            "type": "function",
            "function": {"name": fn.get("name") or "",
                         "arguments": fn.get("arguments") or ""},
        })
    if calls:
        out["tool_calls"] = calls
    return out, reasoning


# ------ Probe ------
def probe(providers, name) -> str:
    """单 provider 连通探针(api_probe.py 的 urllib 化): 打一轮 'ping', 返回人话结论."""
    p = providers.get(name)
    if p is None:
        return "%s: providers.json 里没有这家" % name
    if not p.available:
        return "%s: 环境变量 %s 未设置(无 key 不花钱, 也不发请求)" % (name, p.key_env)
    payload = p.build_payload([{"role": "user", "content": "ping. 只回一个 pong, 别多说."}])
    r = p.send(payload)
    if r is None:
        return "%s: FAIL %s" % (name, p.last_error)
    return "%s: ok · %s · %.1fs · in=%s out=%s · 回复: %s" % (
        name, p.model, r.latency_ms / 1000.0,
        r.usage.get("prompt_tokens", "?"), r.usage.get("completion_tokens", "?"),
        (r.message.get("content") or "").strip()[:60])


def fetch_models(p, timeout: int = 30) -> list:
    """该家真实可调型号清单: GET {base_url}/models (OpenAI 兼容; deepseek/glm 实测可用).

    返回去重保序的 id 列表; 网络/形状异常原样上抛, 由 UI 层显示原因 + 重试."""
    req = urllib.request.Request(p.base_url + "/models")
    if p.key:
        req.add_header("Authorization", "Bearer " + p.key)
    with _open(req, timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out, seen = [], set()
    for m in (data.get("data") or []):
        mid = m.get("id") if isinstance(m, dict) else None
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    if not out:
        raise ValueError("响应里没有 data[].id 模型清单")
    return out

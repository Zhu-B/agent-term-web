# -*- coding: utf-8 -*-
"""vision.py — M15 视觉层: 图像怎么进模型请求 / trace 怎么脱敏 / 图像生成 HTTP.

分工:
  api_messages  回填缝的"每次请求现造"发送视图(canonical messages 永不动):
                  视觉档 → 各 tool 块之后注入一条合成 user 图像消息
                  (两家官方文档只认 user 里的 image_url, tool 内嵌无文档支持);
                  视觉档 → user 消息自带的 _images 直接并进该消息 content
                  (M32 识图委派: spawn 带图 / look_image 的进图缝);
                  非视觉档 → 剥引用 + tool 文本尾注"已略过" + user 消息同注记。
  scrub_images  红线 4(图像本体绝不进 trace/事件流): data URI → "media:<sha1>" 占位;
                  不改原件(send 用的仍是真的), 只复制含图的消息。
  ref_from_path 本地图片文件 → 媒体池 ref(嗅 mime, sha1 去重); M32 识图入口。
  post_generation / fetch_url  图像生成 HTTP, 模块级可打桩(自检不联网)。

大小闸: 单图 base64 > IMG_CAP_B64 不附(注记建议 crop=chat 缩小范围);
每轮最多 IMG_MAX_PER_TURN 张(默认模型来不及看的图只会烧 token)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

import instr
import tools

IMG_CAP_B64 = 6 * 1024 * 1024      # 单图 base64 字符上限(全屏 5K PNG≈5.9M 勉强过)
IMG_MAX_PER_TURN = 2               # 每轮最多附图张数


# ------ 发送视图(canonical → API) ------
def _data_uri(ref):
    """ref.path 的文件 → data URI; 文件缺失返 None(降级跳过, 不炸)."""
    try:
        with open(ref.get("path") or "", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return "data:%s;base64,%s" % (ref.get("mime") or "image/png",
                                  base64.b64encode(raw).decode("ascii"))


def api_messages(messages, capable):
    """canonical messages → 发送视图(浅拷贝列表; 原列表/原消息对象不动).

    capable(当前模型是视觉档) 时: 每个"连续 tool 块"收集其 _images, 块后注入一条
    合成 user 图像消息 —— 保持 assistant(tool_calls)→tool×N→user(图) 结构合法;
    user 消息自带的 _images(识图委派缝) 并进该消息自己的 content。
    非视觉档: 剥掉全部 _images, 有图的 tool/user 文本追加"已略过"尾注。"""
    out = []
    block_refs = []                       # 当前 tool 块攒的图 ref(跨多条 tool 消息)

    def flush():
        if not block_refs:
            return
        parts = [{"type": "text", "text": "(以下是工具返回的图像)"}]
        used = skipped = 0
        for ref in list(block_refs):
            if used >= IMG_MAX_PER_TURN:
                skipped += 1
                continue
            uri = _data_uri(ref)
            if uri is None:
                skipped += 1
                continue
            if len(uri) > IMG_CAP_B64:
                skipped += 1
                continue
            parts.append({"type": "image_url", "image_url": {"url": uri}})
            used += 1
        block_refs.clear()
        if skipped:
            parts[0]["text"] += ("(%d 张超每轮张数或体积上限未附; 截图可用 "
                                 "crop=chat 缩小范围)" % skipped)
        if used or skipped:               # 全被闸/全缺失也要注记: 模型得知道"有图没给"
            out.append({"role": "user", "content": parts})

    for m in messages:
        role = m.get("role")
        m2 = dict(m)
        refs = m2.pop("_images", None)    # 私有键永不出门
        if role != "tool" and block_refs:
            flush()                       # tool 块结束(下一条非 tool) → 图消息插在块后
        if role == "tool" and refs:
            if capable:
                block_refs.extend(refs)
            else:
                m2["content"] = (m2.get("content") or "") + \
                    "\n(附图 %d 张, 当前模型不支持看图, 已略过)" % len(refs)
        if role == "user" and refs:
            if capable:                   # M32: 并进本消息 content(文本+图 parts)
                base = m2.get("content")
                parts = list(base) if isinstance(base, list) else \
                    [{"type": "text", "text": base or ""}]
                used = skipped = 0
                for ref in refs:
                    if used >= IMG_MAX_PER_TURN:
                        skipped += 1
                        continue
                    uri = _data_uri(ref)
                    if uri is None or len(uri) > IMG_CAP_B64:
                        skipped += 1
                        continue
                    parts.append({"type": "image_url", "image_url": {"url": uri}})
                    used += 1
                if skipped:
                    parts.append({"type": "text",
                                  "text": "(%d 张超每轮张数或体积上限未附)" % skipped})
                m2["content"] = parts
            else:                         # 现状曾静默丢弃 → 必须让模型知道有图没给
                m2["content"] = (m2.get("content") or "") + \
                    "\n(这条消息带 %d 张图, 当前模型不支持看图, 已略过)" % len(refs)
        out.append(m2)
    if block_refs:
        flush()
    return out


# ------ trace 脱敏(红线 4) ------
def scrub_images(payload):
    """(脱敏副本, refs): payload.messages 里 image_url 的 data URI 换成
    "media:<sha1>|mime|bytesB" 占位。不改原件 —— send 用的仍是真的。
    只复制含图的那几条消息 dict, 其余共享引用(大 payload 零多余拷贝)。"""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload, []
    refs, new_msgs = [], None
    for i, m in enumerate(msgs):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        new_parts = None
        for j, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url") or ""
            if not url.startswith("data:"):
                continue              # 已是占位/外链: 原样
            try:
                head, b64 = url.split(",", 1)
                raw = base64.b64decode(b64)
            except Exception:
                continue
            sha = hashlib.sha1(raw).hexdigest()
            mime = (head[5:].split(";")[0] or "image/png")
            refs.append({"mime": mime, "sha1": sha, "bytes": len(raw)})
            if new_parts is None:
                new_parts = list(content)
            new_parts[j] = {"type": "image_url", "image_url": {
                "url": "media:%s|%s|%dB" % (sha, mime, len(raw))}}
        if new_parts is not None:
            if new_msgs is None:
                new_msgs = list(msgs)
            mm = dict(m)
            mm["content"] = new_parts
            new_msgs[i] = mm
    if new_msgs is None:
        return payload, refs
    p2 = dict(payload)
    p2["messages"] = new_msgs
    return p2, refs


# ------ 图像生成 HTTP(模块级可打桩) ------
def _open(req, timeout):
    """直连优先(与 provider._open 同哲学; 生成端点多为国内直连)。"""
    return urllib.request.urlopen(req, timeout=timeout)


def post_generation(url, key, body, timeout=180):
    """POST JSON(Bearer) → (ok, data|err). 自检里整个函数被替换, 不联网。"""
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (key or "")},
        method="POST")
    try:
        with _open(req, timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = ex.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return False, "HTTP %s%s" % (ex.code, (": " + detail) if detail else "")
    except Exception as ex:
        return False, "%s: %s" % (type(ex).__name__, ex)


def fetch_url(url, timeout=180):
    """下载图片 URL 响应路 → (ok, bytes|err). 模块级可打桩。"""
    try:
        with _open(url, timeout) as r:
            return True, r.read()
    except Exception as ex:
        return False, "%s: %s" % (type(ex).__name__, ex)


def _sniff_mime(raw, fallback="image/png"):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def ref_from_path(path):
    """本地图片文件 → 媒体池 ref(嗅 mime; save_media 按内容 sha1 去重).
    读不到/空文件返 None —— 调用方回喂人话错误。M32 识图入口(spawn 带图/look_image)."""
    try:
        with open(path or "", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    return instr.save_media(base64.b64encode(raw).decode("ascii"),
                            _sniff_mime(raw))


def run_image_gen(target, prompt, image_path=None):
    """文生图/图生图: target={base_url,key,model} → ToolRet(images=[ref]).

    端点为 OpenAI 兼容 POST {base}/images/generations {model,prompt,n:1,(image)}。
    响应取 data[0].b64_json 直接用 / data[0].url 下载 —— 覆盖智谱 cogview(回 url)、
    硅基/中转站的两种形态。图生图 = image 传 data URI(端点支持图像编辑的型号才认)。"""
    base = (target.get("base_url") or "").rstrip("/")
    if not base:
        return tools.ToolRet(False, "画图档缺少 base_url(去「模型」页检查该条目)")
    body = {"model": target.get("model") or "", "prompt": prompt, "n": 1}
    if image_path:
        try:
            with open(image_path, "rb") as f:
                raw = f.read()
        except OSError as ex:
            return tools.ToolRet(False, "读不到输入图: %s" % ex)
        body["image"] = "data:%s;base64,%s" % (
            _sniff_mime(raw), base64.b64encode(raw).decode("ascii"))
    ok, data = post_generation(base + "/images/generations", target.get("key"), body)
    if not ok:
        return tools.ToolRet(False, "图像生成请求失败: %s" % data)
    item = {}
    try:
        item = (data.get("data") or [{}])[0] or {}
    except Exception:
        pass
    ref = None
    if item.get("b64_json"):
        ref = instr.save_media(item["b64_json"], "image/png")
    elif item.get("url"):
        okd, got = fetch_url(item["url"])
        if not okd:
            return tools.ToolRet(False, "生成成功但下载图片失败: %s" % got)
        ref = instr.save_media(base64.b64encode(got).decode("ascii"),
                               _sniff_mime(got))
    if ref is None:
        return tools.ToolRet(False, "响应里没有可用图像(b64_json/url 都没有): %s"
                             % json.dumps(data, ensure_ascii=False)[:200])
    return tools.ToolRet(True, "已生成图(存媒体池): %s" % ref["path"],
                         images=[ref])

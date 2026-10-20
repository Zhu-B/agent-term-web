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
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import zlib

import instr
import tools

IMG_CAP_B64 = 6 * 1024 * 1024      # 单图 base64 字符上限(全屏 5K PNG≈5.9M 勉强过)
IMG_MAX_PER_TURN = 2               # 每轮最多附图张数
_VIS_MIMES = ("image/png", "image/jpeg", "image/webp")   # 视觉模型认的三件套
# GIF 原图等(动图/媒体原件)随 refs 走仅供展示, 不喂模型(api_messages 里按此过滤)


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
            if (ref.get("mime") or "image/png") not in _VIS_MIMES:
                skipped += 1            # GIF 原图等仅供展示, 不喂模型
                continue
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
                    if (ref.get("mime") or "image/png") not in _VIS_MIMES:
                        skipped += 1    # GIF 原图等仅供展示, 不喂模型
                        continue
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
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return fallback


# ------ 媒体解码(2026-10-20: 读 GIF/视频) ------
def _png_encode(width, height, rgba_rows):
    """RGBA 行(bytes, 每行 width*4)→ PNG bytes. 纯 stdlib(zlib+struct+CRC32),
    滤镜一律 0 —— 简单可靠, 视觉模型不看压缩率."""
    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rgba_rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _gif_lzw(min_code_size, data):
    """GIF LZW 解码 → 像素索引流(bytes). 码宽从 min_code_size+1 起;
    clear 码=2**min_code_size, EOI=clear+1(标准固定). 表含 clear/EOI 两个占位."""
    clear = 1 << min_code_size
    eoi = clear + 1
    cur_w = min_code_size + 1
    table = [bytes([i]) for i in range(clear)] + [None, None]
    out = []
    prev = None
    acc = 0
    nbits = 0
    i = 0
    n = len(data)
    while i < n:
        acc |= data[i] << nbits
        nbits += 8
        i += 1
        while nbits >= cur_w:
            code = acc & ((1 << cur_w) - 1)
            acc >>= cur_w
            nbits -= cur_w
            if code == clear:
                table = [bytes([i]) for i in range(clear)] + [None, None]
                cur_w = min_code_size + 1
                prev = None
                continue
            if code == eoi:
                return b"".join(out)
            if code < len(table) and table[code] is not None:
                entry = table[code]
            elif code == len(table) and prev is not None:
                entry = prev + prev[:1]              # KwKwK 特例
            else:
                raise ValueError("GIF LZW 数据损坏(code=%d)" % code)
            if prev is not None:
                table.append(prev + entry[:1])
                if len(table) == (1 << cur_w) and cur_w < 12:
                    cur_w += 1
            out.append(entry)
            prev = entry
    return b"".join(out)


def _gif_frames_pngs(raw, max_frames):
    """GIF bytes → [(png_bytes, delay_ms)] 前 max_frames 帧(纯 stdlib).
    口径: 非隔行(隔行 GIF 少见, 报错请转 PNG); 每帧独立输出(不做帧间合成,
    局部帧按局部尺寸出); 透明索引 → RGBA a=0."""
    if raw[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("不是 GIF")
    off = 6
    width, height = struct.unpack_from("<HH", raw, off)
    off += 4
    packed = raw[off]
    off += 1
    off += 2                                     # bg index + aspect(在色表前!)
    if packed & 0x80:                            # 全局色表
        gct_size = 2 << (packed & 0x07)
        gct = raw[off:off + 3 * gct_size]
        off += 3 * gct_size
    else:
        gct = b""
    frames = []
    trans_index = None
    while off < len(raw):
        b = raw[off]
        off += 1
        if b == 0x21:                            # 扩展块
            lab = raw[off]
            off += 1
            if lab == 0xF9:                      # GCE: 透明索引/延时
                off += 1                         # block size(4)
                flags = raw[off]
                off += 1
                off += 2                         # delay(用不上)
                t = raw[off]
                off += 1
                trans_index = t if (flags & 1) else None
                off += 1                         # 块终止 0
            else:                                # 其它扩展: 跳子块
                while True:
                    sz = raw[off]
                    off += 1
                    if sz == 0:
                        break
                    off += sz
        elif b == 0x3B:                          # trailer
            break
        elif b == 0x2C:                          # 图像描述符
            _left, _top, iw, ih = struct.unpack_from("<HHHH", raw, off)
            off += 8
            ipacked = raw[off]
            off += 1
            if ipacked & 0x40:
                raise ValueError("隔行 GIF 暂不支持(先用工具转成 PNG 再看)")
            palette = gct
            if ipacked & 0x80:                   # 局部色表
                lct_size = 2 << (ipacked & 0x07)
                palette = raw[off:off + 3 * lct_size]
                off += 3 * lct_size
            min_code_size = raw[off]
            off += 1
            data = bytearray()                   # LZW 子块拼接
            while True:
                sz = raw[off]
                off += 1
                if sz == 0:
                    break
                data += raw[off:off + sz]
                off += sz
            idx = _gif_lzw(min_code_size, bytes(data))
            if len(idx) < iw * ih:
                raise ValueError("GIF 帧数据不完整")
            idx = idx[:iw * ih]
            rows = []
            for y in range(ih):
                row = bytearray()
                for x in range(iw):
                    ci = idx[y * iw + x]
                    if trans_index is not None and ci == trans_index:
                        row += b"\x00\x00\x00\x00"
                    elif 3 * ci + 2 < len(palette):
                        row += palette[3 * ci:3 * ci + 3] + b"\xff"
                    else:
                        row += b"\x00\x00\x00\xff"
                rows.append(bytes(row))
            frames.append((_png_encode(iw, ih, rows), 0))
            if len(frames) >= max_frames:
                break
        else:
            raise ValueError("GIF 结构不认识(offset %d byte 0x%02x)"
                             % (off - 1, b))
    if not frames:
        raise ValueError("GIF 里没有图像帧")
    return frames


def _gif_frames(raw, max_frames):
    """GIF → PNG 帧列表. PIL 在就走 PIL(隔行/帧间合成都稳), 没有走纯 stdlib."""
    try:
        import io as _io
        import PIL.Image
        im = PIL.Image.open(_io.BytesIO(raw))
        frames = []
        for i in range(getattr(im, "n_frames", 1)):
            if len(frames) >= max_frames:
                break
            im.seek(i)
            buf = _io.BytesIO()
            im.convert("RGBA").save(buf, "PNG")
            frames.append((buf.getvalue(), 0))
        return frames
    except Exception:
        pass                                   # PIL 不在或打不开: 走纯 stdlib
    return _gif_frames_pngs(raw, max_frames)


FFMPEG = "ffmpeg"          # 测试缝: 自检改成 stub 脚本路径
FFPROBE = "ffprobe"
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v",
              ".flv", ".ts", ".3gp", ".wmv")


def _video_probe(path):
    """ffprobe 元数据 → (dict(duration,width,height,fps), "") 或 (None, 错误)."""
    if not shutil.which(FFPROBE):
        return None, ("服务器没装 ffmpeg/ffprobe —— 读视频需要它"
                      "(apt install ffmpeg), 或改传截图")
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration:stream=width,height,r_frame_rate",
             "-of", "json", path], capture_output=True, timeout=30)
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception as ex:
        return None, "ffprobe 失败: %s" % ex
    st = None
    for s in data.get("streams") or []:
        if s.get("width"):
            st = s
            break
    if st is None:
        return None, "没探到视频流"
    try:
        num, den = str(st.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / max(1.0, float(den or 1))
    except Exception:
        fps = 0.0
    return {"duration": float((data.get("format") or {}).get("duration") or 0),
            "width": st.get("width"), "height": st.get("height"),
            "fps": fps}, ""


def _video_frames(path, n, meta):
    """ffmpeg 抽 n 帧 → (png_bytes 列表, "") 或 (None, 错误). 临时目录用完即删."""
    if not shutil.which(FFMPEG):
        return None, ("服务器没装 ffmpeg —— 读视频需要它(apt install ffmpeg),"
                      " 或改传截图")
    tmp = tempfile.mkdtemp(prefix="atweb_vid_")
    try:
        dur = max(meta.get("duration") or 0, 0.1)
        rate = "%.6f" % (n / dur)
        cmd = [FFMPEG, "-y", "-i", path, "-vf",
               "scale='min(1280,iw)':-2,fps=%s" % rate,
               "-frames:v", str(n), os.path.join(tmp, "f%02d.png")]
        out = subprocess.run(cmd, capture_output=True, timeout=120)
        pngs = []
        for name in sorted(os.listdir(tmp)):
            if name.endswith(".png"):
                with open(os.path.join(tmp, name), "rb") as f:
                    pngs.append(f.read())
        if not pngs:
            return None, ("ffmpeg 抽帧没产出: %s"
                          % out.stderr.decode("utf-8", "replace")[-200:])
        return pngs, ""
    except Exception as ex:
        return None, "ffmpeg 抽帧失败: %s" % ex
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def refs_from_path(path, frames=2):
    """本地图片/视频文件 → (refs, meta). GIF 动图/视频抽至 frames 帧
    (钳到 IMG_MAX_PER_TURN); 视频需要服务器装 ffmpeg. 失败返 (None, 错误人话)."""
    try:
        with open(path or "", "rb") as f:
            raw = f.read()
    except OSError:
        return None, "读不到文件(路径对? 相对路径相对当前工作目录)"
    if not raw:
        return None, "空文件"
    nf = max(1, min(int(frames or 2), IMG_MAX_PER_TURN))
    mime = _sniff_mime(raw, "")     # 空=不认识: 不兜底成 png, 才能走到视频/报错
    if mime == "image/gif":
        try:
            pngs = _gif_frames(raw, nf)
        except Exception as ex:
            return None, "GIF 解码失败: %s" % ex
        # 原图动图随行(仅供展示: 聊天流 <img> 会播; 模型只吃 PNG 帧)
        refs = [instr.save_media(base64.b64encode(raw).decode("ascii"),
                                 "image/gif")]
        refs += [instr.save_media(base64.b64encode(p).decode("ascii"),
                                  "image/png") for p, _d in pngs]
        meta = ("GIF 动图, 已抽 %d 帧" % len(pngs)) if len(pngs) > 1 \
            else "GIF(静态, 取首帧)"
        return refs, meta
    if mime in ("image/png", "image/jpeg", "image/webp"):
        return [instr.save_media(base64.b64encode(raw).decode("ascii"),
                                 mime)], ""
    if os.path.splitext(path)[1].lower() in VIDEO_EXTS:
        meta, err = _video_probe(path)
        if err:
            return None, err
        pngs, err = _video_frames(path, nf, meta)
        if err:
            return None, err
        refs = [instr.save_media(base64.b64encode(p).decode("ascii"),
                                 "image/png") for p in pngs]
        return refs, ("视频 %.1fs · %dx%d · %.1ffps · 已抽 %d 帧"
                      % (meta.get("duration") or 0, meta.get("width") or 0,
                         meta.get("height") or 0, meta.get("fps") or 0,
                         len(refs)))
    return None, ("不认识的文件类型(只支持 png/jpg/webp/gif/常见视频 "
                  "mp4·webm·mov 等) —— 先用别的工具转成 PNG 再看")


def ref_from_path(path):
    """本地图片 → 媒体池单 ref(GIF/视频取可喂模型的那张 —— 原图动图 ref 在
    列表头仅供展示, 这里取尾部的 PNG 帧). spawn 带图与 image_gen 图生图输入的
    入口. 读不到返 None —— 调用方回喂人话错误."""
    refs, _meta = refs_from_path(path, frames=1)
    return refs[-1] if refs else None


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
        imime = _sniff_mime(raw, "")
        if imime == "image/gif":        # 图生图端点不认 GIF: 取首帧转 PNG
            try:
                raw = _gif_frames(raw, 1)[0][0]
                imime = "image/png"
            except Exception:
                pass                    # 转不动就原样发, 端点自己拒
        body["image"] = "data:%s;base64,%s" % (
            imime or "image/png", base64.b64encode(raw).decode("ascii"))
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

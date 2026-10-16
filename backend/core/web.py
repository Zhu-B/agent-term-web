# -*- coding: utf-8 -*-
"""web.py — 联网工具后端(M22): web_fetch 抓页转文本 / web_search 搜索.

纯标准库. tools.py 的两个工具只做参数校验与风险门, 网络与解析全在这层.

设计口径(与 Claude Code 的 WebFetch/WebSearch 对齐, 差异处注明):
- fetch: 直连优先、网络层失败回退系统代理(provider._open 的 M8 系统代理坑同源教训);
  超时 20s, 单次最多读 2MB, 同主机重定向自动跟(≤3 跳), **跨主机重定向不自动跟**
  —— 返回 redirect 回执让模型确认后用新地址重调(CC 同口径, 防开放重定向被带着走).
- SSRF 守卫: 解析主机全部地址, 任一命中非公网(回环/私网/链路本地/ULA/0.0.0.0)即拒
  (防被网页内容诱导打内网). 每一跳(含重定向目标)都过同一道守卫.
- html→文本: html.parser 剥 script/style/noscript/template/svg, 块级标签转换行,
  空白折叠, 截 12000 字符; 非 html(纯文本/json/csv)原样截断. 附 title 与最终 URL.
- search: 双后端链 **Bing(国内直连) → DuckDuckGo(代理环境)**, 免 key.
  2026-10-15 本机实测: cn.bing.com 直连 200/0.5s 且结果块稳定(li.b_algo/h2>a/
  b_caption>p); html.duckduckgo.com 直连与系统代理双灭. Bing 的链接偶尔是
  bing.com/ck/a?u=<base64url> 跳转, 解包还原; DDG 的链接是 //duckduckgo.com/l/?uddg=
  <enc> 包裹, 解包还原. 上游限流/改版会失败, 如实报错不幻觉结果.

测试缝(全模块级属性, _verify 的 web_selfcheck 就地替换):
  _open      单次抓取实现(默认 _fetch_once, 返回 (status, final_url, ctype, body))
  _resolve   域名解析(默认 socket.getaddrinfo, 测 SSRF 时注假地址)
  _search_fetch  搜索后端链(默认 _search_chain, 返回 (backend, html); 测解析时注固定页)
  _bing_fetch / _ddg_fetch  单后端抓取(测"链式回退"时替换)
"""
from __future__ import annotations

import base64
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

FETCH_TIMEOUT = 20.0
MAX_BYTES = 2_000_000          # 单次最多读的字节数(多读 1 字节用于判断截断)
MAX_TEXT_CHARS = 12000         # 回喂模型的正文字符上限
MAX_REDIRECTS = 3              # 同主机重定向最多跟几跳
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")   # 裸 python-ua 常被拒
SEARCH_URL = "https://html.duckduckgo.com/html/?q="


class WebError(Exception):
    """给人看的联网失败(直接进 ToolRet.content 回喂模型)."""


# ------ 抓取(直连优先, 回退系统代理) ------

class _CrossHostRedirect(Exception):
    """跨主机重定向: 不自动跟, 把目标带回去让调用方决定."""

    def __init__(self, location):
        super().__init__(location)
        self.location = location


class _RedirectStop(urllib.request.HTTPRedirectHandler):
    """同主机重定向照常跟(≤MAX_REDIRECTS 跳); 跨主机的抛 _CrossHostRedirect."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        old_key = (old.scheme.lower(), (old.hostname or "").lower())
        new_key = (new.scheme.lower(), (new.hostname or "").lower())
        if old_key != new_key:
            raise _CrossHostRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_once(url, timeout=FETCH_TIMEOUT):
    """单次抓取. 返回 (status, final_url, content_type, body_bytes).

    直连优先: 系统代理可能是坏的(M8 教训), 网络层失败(不是 HTTP 4xx/5xx)再回退
    系统代理重试一次. HTTP 错误码原样抛(urllib 的 HTTPError), 由调用方归类.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    last_exc = None
    attempts = ({"http": None, "https": None},   # 显式清空 = 直连
                None)                            # None = 读系统代理
    for proxies in attempts:
        handler = urllib.request.ProxyHandler(proxies) if proxies else \
            urllib.request.ProxyHandler()
        opener = urllib.request.build_opener(handler, _RedirectStop())
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
                return resp.getcode(), resp.geturl(), \
                    resp.headers.get("Content-Type", ""), body
        except _CrossHostRedirect:
            raise                                # 不是网络失败, 不吃回退
        except urllib.error.HTTPError:
            raise                                # HTTP 层错误也不吃回退
        except Exception as exc:                 # 连接/超时/TLS 等网络层
            last_exc = exc
    raise last_exc


# 打桩缝(测试替换)
_open = _fetch_once
_resolve = socket.getaddrinfo


# ------ SSRF 守卫 ------

def _assert_public_host(url):
    """URL 的主机必须解析到全公网地址; 任一非公网即拒."""
    split = urllib.parse.urlsplit(url)
    if split.scheme not in ("http", "https"):
        raise WebError("只支持 http/https 地址, 收到: %s" % split.scheme)
    host = split.hostname
    if not host:
        raise WebError("URL 里没有主机名")
    if host.lower() in ("localhost",) or host.endswith(".localhost"):
        raise WebError("拒绝访问 localhost(防 SSRF)")
    port = split.port or (443 if split.scheme == "https" else 80)
    try:
        infos = _resolve(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise WebError("域名解析失败(%s): %s" % (host, exc))
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw.split("%", 1)[0])  # 剥 zone id
        except ValueError:
            raise WebError("无法识别的地址(%s), 拒绝访问" % raw)
        if not addr.is_global:
            raise WebError("拒绝访问非公网地址 %s(防 SSRF; 解析自 %s)" % (addr, host))


def _normalize_url(url):
    url = str(url or "").strip()
    if not url:
        raise WebError("缺少 url")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url                     # 裸域名默认升 https(CC 同款)
    return url


# ------ html → 文本 ------

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "aside", "table", "thead", "tbody",
               "blockquote", "pre", "ul", "ol", "dl", "dt", "dd", "hr",
               "header", "footer", "nav", "form", "figure", "figcaption", "main"}


class _TextExtract(HTMLParser):
    """剥脚本样式、块级换行、收集 title. convert_charrefs 顺手做实体解码."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def html_to_text(html, max_chars=MAX_TEXT_CHARS):
    """html → 纯文本(压缩空白 + 截断). 返回 (title, text, truncated)."""
    parser = _TextExtract()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass   # 烂 html 不致命, 收多少算多少
    raw = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", raw)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    truncated = len(text) > max_chars
    title = parser.title.strip()
    if truncated:
        text = text[:max_chars]
    return title, text, truncated


def _decode_body(body, ctype):
    """按 Content-Type 的 charset 解码, 缺省 utf-8(容错替换)."""
    enc = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    if m:
        enc = m.group(1)
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ------ fetch 主入口 ------

def fetch(url, timeout=FETCH_TIMEOUT):
    """抓网页转文本. 返回 dict(ok/status/final_url/title/text/truncated/
    redirect/error/bytes). 调用方(tools)负责拼给人/模型看的 content."""
    out = {"ok": False}
    try:
        url = _normalize_url(url)
        _assert_public_host(url)
        status, final_url, ctype, body = _open(url, timeout=timeout)
    except _CrossHostRedirect as exc:
        out["redirect"] = exc.location
        out["error"] = ("重定向到其它主机: %s —— 若可信, 用这个新地址重新调 web_fetch"
                        % exc.location)
        return out
    except WebError as exc:
        out["error"] = str(exc)
        return out
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["error"] = "HTTP %s %s" % (exc.code, exc.reason)
        return out
    except Exception as exc:
        out["error"] = "抓取失败(%s): %s" % (type(exc).__name__, exc)
        return out

    out["status"] = status
    out["final_url"] = final_url
    out["bytes"] = len(body)
    over = len(body) > MAX_BYTES
    if over:
        body = body[:MAX_BYTES]
    if "html" in (ctype or "").lower():
        title, text, truncated = html_to_text(_decode_body(body, ctype))
    else:
        title, truncated = "", over
        text = _decode_body(body, ctype)
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
    out["title"] = title
    out["text"] = text
    out["truncated"] = bool(truncated or over)
    out["ok"] = status is not None and 200 <= int(status) < 400
    if out["ok"] and not text.strip():
        out["ok"] = False
        out["error"] = "抓到了(%d 字节)但没提取到正文(可能是纯交互页)" % len(body)
    return out


# ------ search(Bing 主后端, DDG 兜底) ------

BING_URL = "https://cn.bing.com/search?q="


def _ddg_fetch(query):
    """DDG 后端: 免 key, 国内直连不通(实测), 代理环境可用."""
    url = SEARCH_URL + urllib.parse.quote_plus(query)
    _assert_public_host(url)
    status, _final, ctype, body = _open(url)
    if not (200 <= int(status) < 400):
        raise WebError("ddg HTTP %s" % status)
    return _decode_body(body, ctype)


def _bing_fetch(query):
    """Bing 后端: 国内直连实测 200/0.5s, 结果块 li.b_algo 稳定."""
    url = BING_URL + urllib.parse.quote_plus(query) + "&count=10"
    _assert_public_host(url)
    status, _final, ctype, body = _open(url)
    if not (200 <= int(status) < 400):
        raise WebError("bing HTTP %s" % status)
    return _decode_body(body, ctype)


def _search_chain(query):
    """后端链: bing 直连优先, 失败退 ddg. 返回 (backend, html); 全灭如实抛."""
    errors = []
    for name, fetch in (("bing", _bing_fetch), ("ddg", _ddg_fetch)):
        try:
            return name, fetch(query)
        except Exception as exc:
            errors.append("%s(%s)" % (name, exc))
    raise WebError("搜索后端均不可用: " + "; ".join(errors))


# 打桩缝
_search_fetch = _search_chain


def _unwrap_ddg(href):
    """DDG 结果链接解包: //duckduckgo.com/l/?uddg=<enc> → 真实地址."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    split = urllib.parse.urlsplit(href)
    if "duckduckgo.com" in (split.netloc or "").lower():
        qs = urllib.parse.parse_qs(split.query)
        if "uddg" in qs:
            return urllib.parse.unquote(qs["uddg"][0])
        return ""                       # 站内跳转(如广告)不给出
    if split.scheme in ("http", "https"):
        return href
    return ""


class _DDGParser(HTMLParser):
    """收 result__a(标题+链接) 与 result__snippet(摘要).

    DDG 的摘要在标题链接闭合之后才出现, 所以用「最近完成的 result」持有者接摘要,
    不能用"当前未闭合链接"判定(那是 None).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._cur = None
        self._last = None             # 最近一条完成的 result__a, 摘要归它
        self._mode = None             # "a" | "snip" | None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        cls = (dict(attrs).get("class") or "").split()
        if "result__a" in cls:
            url = _unwrap_ddg(dict(attrs).get("href", ""))
            self._cur = {"title": "", "url": url, "snippet": ""}
            self._mode = "a"
        elif "result__snippet" in cls and self._last is not None:
            self._mode = "snip"

    def handle_data(self, data):
        if self._mode == "a" and self._cur is not None:
            self._cur["title"] += data
        elif self._mode == "snip" and self._last is not None:
            self._last["snippet"] += data

    def handle_endtag(self, tag):
        if tag != "a":
            return
        if self._mode == "a" and self._cur is not None:
            if self._cur["title"].strip() and self._cur["url"]:
                self._last = self._cur
                self.results.append(self._cur)
            self._cur = None
        self._mode = None


def _unwrap_bing(href):
    """Bing 结果链接解包: bing.com/ck/a?u=<base64url> → 真实地址; 站内链接丢弃."""
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "#", "/")):
        return ""
    split = urllib.parse.urlsplit(href)
    if "bing.com/ck/" in (split.path or "") or "bing.com/ck/" in href:
        qs = urllib.parse.parse_qs(split.query)
        if "u" not in qs:
            return ""
        raw = qs["u"][0]
        try:
            pad = raw + "=" * (-len(raw) % 4)
            real = base64.urlsafe_b64decode(pad).decode("utf-8", errors="replace")
        except Exception:
            return ""
        return real if real.startswith(("http://", "https://")) else ""
    if split.scheme in ("http", "https"):
        return href
    return ""


class _BingParser(HTMLParser):
    """收 <li class="b_algo"> 的 标题链接(h2 内首个 <a>) 与摘要(其后的 <p>).

    标题里常嵌 <strong>, 走 handle_data 天然拼回来; 结果块前有一堆 <link> 样式,
    不影响(只认 h2/li/p).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._in_algo = 0
        self._in_h2 = False
        self._cur = None
        self._mode = None              # "a" | "snip" | None

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = (attrs_d.get("class") or "").split()
        if tag == "li" and "b_algo" in cls:
            self._in_algo += 1
        elif not self._in_algo:
            return
        elif tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and self._cur is None:
            self._cur = {"title": "", "url": _unwrap_bing(attrs_d.get("href", "")),
                         "snippet": ""}
            self._mode = "a"
        elif tag == "p" and self._cur is not None:
            self._mode = "snip"

    def handle_data(self, data):
        if self._mode == "a" and self._cur is not None:
            self._cur["title"] += data
        elif self._mode == "snip" and self._cur is not None:
            self._cur["snippet"] += data

    def handle_endtag(self, tag):
        if tag == "li" and self._in_algo:
            self._in_algo -= 1
            if not self._in_algo and self._cur is not None:
                if self._cur["title"].strip() and self._cur["url"]:
                    self.results.append(self._cur)
                self._cur = None
                self._mode = None
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "a" and self._mode == "a":
            self._mode = None
        elif tag == "p" and self._mode == "snip":
            self._mode = None


_SEARCH_PARSERS = {"bing": _BingParser, "ddg": _DDGParser}


def search(query, limit=8):
    """web_search 后端: 返回 {ok, results:[{title,url,snippet}], error}."""
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "缺少搜索词"}
    limit = max(1, min(int(limit or 8), 10))
    try:
        backend, raw = _search_fetch(query)
    except WebError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False,
                "error": "搜索后端不可用(%s): %s" % (type(exc).__name__, exc)}
    parser = _SEARCH_PARSERS.get(backend, _DDGParser)()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    results = [{k: v.strip() for k, v in r.items()} for r in parser.results]
    results = [r for r in results if r["title"] and r["url"]][:limit]
    if not results:
        return {"ok": False,
                "error": "搜索后端 %s 没解析到结果(可能限流或页面改版); "
                         "换词重试或改用 web_fetch 直接抓已知地址" % backend}
    return {"ok": True, "results": results}

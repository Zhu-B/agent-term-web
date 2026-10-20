# -*- coding: utf-8 -*-
"""selfcheck.py — Web 后端内置自检(不联网, 三件套同款精神).

沙箱纪律: keys/models/settings/记忆/账本/定时/会话/trace 全部指临时目录,
绝不碰服务器上的真实配置; provider 用剧本 FakeProvider(照 _verify 先例).
链路: health → 加自增档(掩码) → 建会话 → SSE 收全程事件序列 → perm 三键卡
真问真答 → ask_user 卡真问真答 → 中断 → 静态托管 → token 鉴权.

用法: python server.py --check
"""
from __future__ import annotations

import base64
import json
import os
import queue
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# E1.3: AT_DATA 一根线把全部服务端状态指进沙箱(server 在 import core 前读它)
_SBX = tempfile.mkdtemp(prefix="at_web_check_")
os.environ["AT_DATA"] = _SBX
# E2 沙箱: 版本系统指临时目录(假 backend 树在 ⑰ 现造), 重启命令无害化.
os.environ.setdefault("AT_VERSIONS_DIR", os.path.join(_SBX, "versions"))
os.environ.setdefault("AT_BACKEND_ROOT", os.path.join(_SBX, "backend"))
os.environ.setdefault("AT_RESTART_CMD", "echo")
import server                      # noqa: E402  (顺带把 core/ 挂上 sys.path)
import provider as provider_mod
import session as session_mod
import instr as instr_mod
import memory as memory_mod
import webusers
import version as version_mod
import vision as vision_mod
from provider import ChatResult

# 坑(2026-10-19 实测): server.py 以 __main__ 起跑时, 模块级 import 已把
# version 按真实路径导入并缓存 —— 这里的 env setdefault 改不了它, 假树会
# 写穿到真实 backend! 必须直接改绑模块属性(上面 setdefault 只是口径一致).
version_mod.VERSIONS_DIR = os.path.join(_SBX, "versions")
version_mod.BACKEND_ROOT = os.path.join(_SBX, "backend")
version_mod.RESTART_CMD = "echo"
version_mod.ACTIVE_FILE = os.path.join(version_mod.VERSIONS_DIR, "ACTIVE")
version_mod.RESTART_MARKER = os.path.join(
    version_mod.VERSIONS_DIR, "RESTART.marker")

BASE_URL = None                    # build_server 分到的地址
_SSE_TOKEN = ""                    # 令牌制段起 SSE 要带的 token


# ---------------- 剧本 provider(照 _verify.FakeProvider 精神) ----------------
class FakeProvider(provider_mod.Provider):
    def __init__(self, script=None, name="fake"):
        super().__init__(name, {"base_url": "", "key_env": "AT_FAKE_KEY",
                                "model": "fake-model", "family": "test"})
        self.available = True
        self.key = "sk-fake"
        self.script = list(script or [])
        self.i = 0

    def build_payload(self, messages, tools=None, **extra):
        return {"model": self.model, "messages": messages}

    def send(self, payload, on_chunk=None):
        if self.i >= len(self.script):
            raise RuntimeError("剧本耗尽")
        step = dict(self.script[self.i])
        self.i += 1
        if step.get("_sleep"):
            time.sleep(step.pop("_sleep"))
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        finish = "tool_calls" if step.get("tool_calls") else "stop"
        return ChatResult(message={"role": "assistant", **step}, usage=usage,
                          finish_reason=finish, latency_ms=3,
                          raw={"choices": [{"message": step}], "usage": usage})


def _tc(mid, name, args):
    return {"id": mid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(
                args, ensure_ascii=False)}}


# ---------------- HTTP 小件 ----------------
# 本机若有系统代理, urllib 会把 127.0.0.1 也递给代理(实测必断) —— 直连 opener.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, body=None, method=None, token=None, raw=False):
    url = BASE_URL + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
        if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or (
        "POST" if data is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=25) as r:
            payload = r.read().decode("utf-8")
            return r.status, (payload if raw else json.loads(payload or "{}"))
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read().decode("utf-8") or "{}")


def reset_script(sid, steps):
    """换剧本: 直接改会话持有的 provider(resolve_model 给的是 copy.copy,
    改原件不生效 —— 实测踩过)."""
    s = server.HUB.get(sid)
    p = s.app.provider
    p.script = list(steps)
    p.i = 0


def wait_idle(sid, timeout=10):
    """等会话空下来(turn_end 事件先于 busy 清零发出, 连发会 409 —— 竞态实测)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, r = api("/api/sessions")
        s = next((x for x in r.get("sessions", []) if x["sid"] == sid), None)
        if s is not None and not s["busy"]:
            return True
        time.sleep(0.2)
    return False


class SSE:
    """后台收一个会话的 SSE 流, 事件攒进队列供 wait/drain 消费."""

    def __init__(self, sid):
        self.q = queue.Queue()
        self._stop = False
        url = "%s/api/sessions/%s/events?since=0" % (BASE_URL, sid)
        if _SSE_TOKEN:                    # 令牌制段(E1 后)带上 token
            url += "&token=" + _SSE_TOKEN
        req = urllib.request.Request(url)
        self.resp = _OPENER.open(req, timeout=30)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for ln in self.resp:
                if self._stop:
                    break
                ln = ln.decode("utf-8").strip()
                if ln.startswith("data: "):
                    self.q.put(json.loads(ln[6:]))
        except Exception:
            pass

    def wait(self, pred, timeout=25):
        """等一条满足 pred 的事件; 超时返 None."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                ev = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if pred(ev):
                return ev
        return None

    def drain_until(self, pred, timeout=25):
        """把队列一路喝到 pred 命中, 返回途经的 type 序列(含命中的)."""
        seq, t0 = [], time.time()
        while time.time() - t0 < timeout:
            try:
                ev = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            seq.append(ev.get("type"))
            if pred(ev):
                break
        return seq

    def close(self):
        self._stop = True
        try:
            self.resp.close()
        except Exception:
            pass


# ---------------- 自检主体 ----------------
def main():
    global BASE_URL
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # 沙箱: AT_DATA 已在 import 前指临时目录(models/keys/记忆/账本/用户库/
    # 分域根全部落在里面); 这里只补没有 env 口子的 run 系目录.
    d = _SBX
    saved = []
    for mod, attrs in ((session_mod, ["SESSIONS_DIR"]),
                       (instr_mod, ["TRACE_DIR", "RAW_DIR"])):
        for a in attrs:
            saved.append((mod, a, getattr(mod, a)))
            setattr(mod, a, os.path.join(d, a.lower()))
    old_raw_sink = instr_mod.raw_store_sink
    instr_mod.raw_store_sink = lambda *a, **k: old_raw_sink(
        os.path.join(d, "raw"))
    old_provs = provider_mod.load_providers
    old_customs = provider_mod.custom_providers
    fake = FakeProvider()
    provider_mod.load_providers = lambda: dict(
        {"fake": fake}, **old_customs())

    httpd = server.build_server(0)
    BASE_URL = "http://127.0.0.1:%d" % httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    sse = None
    sid = None
    try:
        # ① health
        code, h = api("/api/health")
        labels = [m["label"] for m in h.get("models", [])]
        chk("health(ok+fake 在清单)", code == 200 and h.get("ok")
            and "fake/fake-model" in labels, str(labels))

        # ② 加自增档 + 掩码红线(key 全串绝不回显)
        code, r = api("/api/models", {
            "label": "自检中转", "base_url": "https://x.invalid/v1",
            "key": "sk-SECRET-NOLEAK", "model": "gx"})
        _, cat = api("/api/models")
        blob = json.dumps([r, cat], ensure_ascii=False)
        chk("自增档落盘+清单可见+key 恒掩码",
            code == 200 and r.get("key_mask")
            and any(m["label"] == "自检中转" for m in cat.get("models", []))
            and "SECRET-NOLEAK" not in blob and "sk-SE" not in blob,
            blob[:120])

        # ③ 建会话 + 全程 SSE 事件序列(一次 read 工具调用 → 结论)
        #    (会话还没建: 剧本落原件上, create 时 resolve 复制带走)
        fake.script = [
            {"content": "", "tool_calls": [
                _tc("t1", "read", {"path": "README.md", "limit": 3})]},
            {"content": "看完了, README 没问题."},
        ]
        fake.i = 0
        code, r = api("/api/sessions", {"model": "fake/fake-model"})
        sid = r.get("sid")
        chk("建会话", code == 200 and bool(sid), str(r)[:120])
        sse = SSE(sid)
        api("/api/sessions/%s/send" % sid, {"text": "看看 README"})
        seq = sse.drain_until(lambda ev: ev.get("type") == "turn_end")
        chk("SSE 全程序列(turn_start→tool→…→turn_end)",
            "turn_start" in seq and "tool_call" in seq
            and "tool_result" in seq and "llm_response" in seq
            and seq.count("turn_end") == 1, str(seq))
        wait_idle(sid)

        # ④ perm 三键卡: bash 触发 → 卡到 → 答 allow → 工具真执行
        reset_script(sid, [
                 {"content": "", "tool_calls": [
                     _tc("t2", "bash", {"command": "echo webperm"})]},
                 {"content": "跑完了."},
             ])
        api("/api/sessions/%s/send" % sid, {"text": "跑个 echo"})
        card = sse.wait(lambda ev: ev and ev.get("type") == "perm_ask")
        ok_card = bool(card) and card.get("name") == "bash"
        if ok_card:
            api("/api/sessions/%s/ask/%s/answer" % (sid, card["id"]),
                {"answer": "allow"})
        fin = sse.wait(lambda ev: ev and ev.get("type") == "tool_result"
                       and "webperm" in str(ev.get("content", "")))
        wait_idle(sid)
        chk("perm 卡(bash 问到→答 allow→真执行)", ok_card and bool(fin),
            str(card)[:120])

        # ⑤ ask_user 选项卡: 问到 → 答选项 → 回合收工
        reset_script(sid, [
                 {"content": "", "tool_calls": [
                     _tc("t3", "ask_user", {
                         "question": "走哪条路?", "header": "选路",
                         "options": [{"label": "快路"}, {"label": "稳路"}]})]},
                 {"content": "选了, 收工."},
             ])
        api("/api/sessions/%s/send" % sid, {"text": "问我要哪条路"})
        card = sse.wait(lambda ev: ev and ev.get("type") == "ask_user")
        ok_card = bool(card) and "快路" in str(card.get("options", []))
        if ok_card:
            api("/api/sessions/%s/ask/%s/answer" % (sid, card["id"]),
                {"label": "快路"})
        fin = sse.wait(lambda ev: ev and ev.get("type") == "turn_end"
                       and ev.get("iters") == 2)
        wait_idle(sid)
        chk("ask_user 卡(问到→答label→回合收工)", ok_card and bool(fin),
            str(card)[:120])

        # ⑥ 中断: 慢回合中途 interrupt → turn_end.aborted
        reset_script(sid, [
                 {"content": "", "_sleep": 2.5, "tool_calls": [
                     _tc("t4", "read", {"path": "README.md", "limit": 2})]},
                 {"content": "不该到这."},
             ])
        api("/api/sessions/%s/send" % sid, {"text": "慢点跑"})
        time.sleep(0.8)
        api("/api/sessions/%s/interrupt" % sid, {})
        ev = sse.wait(lambda ev: ev and ev.get("type") == "turn_end"
                      and ev.get("aborted"))
        chk("中断(turn_end.aborted)", bool(ev), str(ev)[:120])

        # ⑦ 静态托管(同源前端): 指临时目录 → / 出 index
        st = os.path.join(d, "www")
        os.makedirs(st)
        with open(os.path.join(st, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>static-ok</html>")
        server.STATIC_DIR = st
        code, body = api("/", raw=True)
        chk("静态托管(/)", code == 200 and "static-ok" in body, body[:80])

        # ⑧ 鉴权: 设 token 后裸访问 401, 带 token 200
        server.TOKEN = "t123"
        c1, _ = api("/api/health")
        c2, _ = api("/api/health", token="t123")
        server.TOKEN = ""
        chk("token 鉴权(裸=401/带=200)", c1 == 401 and c2 == 200,
            "%s/%s" % (c1, c2))

        # ⑨ M2 忆库: 造真卡 → 清单两区 + 点开读 + 注入会话
        import memory as memory_mod
        memory_mod.write_card("自检卡", "跨会话记忆自检内容 XYZ",
                              zone="archive")
        code, r = api("/api/memory")
        ok_mem = code == 200 and any(
            c["title"] == "自检卡" and c["zone"] == "archive"
            for c in r.get("cards", []))
        _, r = api("/api/memory/card?title=" + urllib.parse.quote("自检卡"))
        ok_mem = ok_mem and r.get("ok") and "XYZ" in (r.get("content") or "")
        chk("忆库API(两区清单+点开读)", ok_mem, str(r)[:120])
        c2, r2 = api("/api/sessions/%s/memory" % sid, {"title": "自检卡"})
        chk("记忆卡注入会话", c2 == 200 and r2.get("ok"), str(r2)[:80])

        # ⑩ M2 技能: 预置技能可见 + 读全文
        code, r = api("/api/skills")
        ok_sk = code == 200 and any(s["name"] == "subcontract-loop"
                                    for s in r.get("skills", []))
        _, r = api("/api/skill?name=subcontract-loop")
        ok_sk = ok_sk and r.get("ok") and len(r.get("body") or "") > 100
        chk("技能API(清单+读全文)", ok_sk, str(r)[:100])

        # ⑪ M2 账本: 造真账 → 明细 + 聚总
        import ledger as ledger_mod
        ledger_mod.append({"ts": time.time(), "sid": "chk", "parent": "p",
                           "sub": "web检查", "model": "fake/fake-model",
                           "task": "自检任务", "ok": True,
                           "tokens": {"prompt_tokens": 1,
                                      "completion_tokens": 1,
                                      "total_tokens": 2},
                           "wall_s": 0.1, "result_head": "自检结果"})
        code, r = api("/api/ledger?limit=50")
        ok_lg = (code == 200
                 and any(e.get("sub") == "web检查"
                         for e in r.get("entries", []))
                 and any(g.get("model") == "fake/fake-model"
                         for g in r.get("aggregate", [])))
        chk("账本API(明细+聚总)", ok_lg, "")

        # ⑫ E1 多用户: 影子超管令牌 → 开户 → 登录 → 越权拦截
        server.TOKEN = "t123"
        c, h = api("/api/health", token="t123")
        ok_admin = c == 200 and h.get("ok")
        c, r = api("/api/admin/users", {"username": "bob",
                                        "password": "bobpass1",
                                        "role": "user"}, token="t123")
        bob_uid = (r.get("user") or {}).get("uid")
        ok_create = c == 200 and r.get("ok") and bool(bob_uid)
        c, r = api("/api/login", {"username": "bob", "password": "bobpass1"})
        bob_tok = (r.get("user") or {}).get("token")
        ok_login = c == 200 and r.get("ok") and bool(bob_tok)
        c, _ = api("/api/login", {"username": "bob", "password": "错的"})
        ok_badpw = c == 401
        c, r = api("/api/me", token=bob_tok)
        ok_me = (c == 200 and r.get("username") == "bob"
                 and r.get("role") == "user")
        c, _ = api("/api/admin/users", token=bob_tok)
        ok_403 = c == 403
        c, _ = api("/api/health")
        ok_notoken = c == 401                        # 开了令牌制后裸访问拒
        chk("E1 鉴权链(影子超管/开户/登录/错密401/越权403/裸拒)",
            ok_admin and ok_create and ok_login and ok_badpw and ok_me
            and ok_403 and ok_notoken, str(r)[:120])

        # ⑬ E1 会话归属: bob 只见自己的, 摸别人的 403
        c, r = api("/api/sessions", {"model": "fake/fake-model"}, token=bob_tok)
        bob_sid = r.get("sid")
        _, r = api("/api/sessions", token=bob_tok)
        n_bob = len(r.get("sessions", []))
        _, r = api("/api/sessions", token="t123")
        n_admin = len(r.get("sessions", []))
        c, _ = api("/api/sessions/%s/send" % sid, {"text": "偷看"},
                   token=bob_tok)
        ok_own = bool(bob_sid) and n_bob >= 1 and n_admin > n_bob and c == 403
        chk("E1 会话归属(自己可见/别人403/超管全见)", ok_own,
            "bob=%d admin=%d rc=%s" % (n_bob, n_admin, c))

        # ⑭ E1 数据分域: bob 的记忆卡/私有档, bob 可见而超管(全局)不见
        server.set_user(bob_uid)
        memory_mod.write_card("bob的卡", "BOB-SECRET", zone="archive")
        server.set_user(None)
        _, r = api("/api/memory", token=bob_tok)
        ok_mem = any(cc["title"] == "bob的卡" for cc in r.get("cards", []))
        _, r = api("/api/memory", token="t123")
        ok_mem = ok_mem and not any(cc["title"] == "bob的卡"
                                    for cc in r.get("cards", []))
        c, r = api("/api/models", {"label": "bob档",
                                   "base_url": "https://x.invalid/v1",
                                   "key": "sk-bob-secret", "model": "bx"},
                   token=bob_tok)
        _, r = api("/api/models", token=bob_tok)
        ok_priv = any(m["label"] == "bob档" for m in r.get("models", []))
        _, r = api("/api/models", token="t123")
        ok_priv = (ok_priv and not any(m["label"] == "bob档"
                                       for m in r.get("models", [])))
        chk("E1 分域(记忆卡/私有模型档互不可见)", ok_mem and ok_priv,
            str(r.get("models"))[:120])

        # ⑮ E1 审计有账(开户/登录成功都在)
        _, r = api("/api/admin/audit", token="t123")
        acts = [a["action"] for a in r.get("audit", [])]
        ok_audit = "user_create" in acts and "login_ok" in acts
        chk("E1 审计有账(开户/登录)", ok_audit, str(acts)[:120])

        # ⑯ E1.2 官方家: 家清单/公共与私有 key/家内档/拉清单(打桩)
        old_chain = provider_mod.PROVIDERS_CHAIN
        old_keystore = provider_mod.KEY_STORE
        old_fetch = provider_mod.fetch_models
        provider_mod.PROVIDERS_CHAIN = [
            os.path.join(server.CORE, "providers.example.json")]
        provider_mod.KEY_STORE = os.path.join(d, "keys.json")
        fetched = {}
        provider_mod.fetch_models = lambda p, timeout=30: (
            fetched.update(key=p.key) or ["dm1", "dm2"])
        provider_mod.load_providers = old_provs   # 用真链(已沙箱化)
        _, r = api("/api/providers", token="t123")
        names = [p["name"] for p in r.get("providers", [])]
        ok_prov = all(n in names for n in ("deepseek", "qwen", "glm"))
        api("/api/providers/key", {"name": "deepseek", "key": "sk-PUB-K"},
            token="t123")
        _, r = api("/api/providers", token="t123")
        dp = next((p for p in r.get("providers", [])
                   if p["name"] == "deepseek"), {})
        ok_pub = dp.get("available") and dp.get("key_src") == "公共"
        api("/api/providers/key", {"name": "qwen", "key": "sk-BOB-K"},
            token=bob_tok)
        _, r = api("/api/providers", token=bob_tok)
        qp = next((p for p in r.get("providers", [])
                   if p["name"] == "qwen"), {})
        _, r2 = api("/api/providers", token="t123")
        qp2 = next((p for p in r2.get("providers", [])
                    if p["name"] == "qwen"), {})
        ok_key = (qp.get("available") and qp.get("key_src") == "私有"
                  and not qp2.get("available"))
        c, r = api("/api/models", {"provider": "qwen", "model": "dm1"},
                   token=bob_tok)
        _, r = api("/api/models", token=bob_tok)
        ok_fam = any(m.get("label") == "qwen/dm1" and m.get("available")
                     for m in r.get("models", []))
        _, r = api("/api/models", token="t123")
        ok_fam = ok_fam and not any(m.get("label") == "qwen/dm1"
                                    for m in r.get("models", []))
        c, r = api("/api/providers/fetch", {"name": "qwen"}, token=bob_tok)
        ok_fetch = (c == 200 and r.get("ok")
                    and r.get("models") == ["dm1", "dm2"]
                    and fetched.get("key") == "sk-BOB-K")
        chk("E1.2 官方家(清单/公共私有key/家内档/拉清单带key)",
            ok_prov and ok_pub and ok_key and ok_fam and ok_fetch,
            str(names)[:120])
        # ⑰ E2 版本系统: 造假树→建v0001→改树建v0002→diff→bob切回v0001
        #    (滚前自动快照/只覆盖永不删除/重启标记/激活指针)
        root = version_mod.BACKEND_ROOT
        assert os.path.realpath(root).startswith(
            os.path.realpath(_SBX) + os.sep), \
            "E2 沙箱未生效(BACKEND_ROOT 不在 _SBX 内), 拒绝动真实树"

        def _w(rel, s):
            fp = os.path.join(root, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(s)
        _w("server.py", "A=1\n")
        _w("core/x.py", "X=1\n")
        c, r = api("/api/versions", {"label": "基线", "note": "首版"},
                   token="t123")
        ok_v1 = c == 200 and (r.get("version") or {}).get("vid") == "v0001"
        _w("server.py", "A=2  # 改了\n")
        _w("extra.py", "E=1\n")
        c, r = api("/api/versions", {"label": "改动"}, token="t123")
        v2 = (r.get("version") or {}).get("vid")
        c, r = api("/api/versions/diff?a=v0001&b=" + v2, token="t123")
        fmap = {f["p"]: f["st"] for f in r.get("files", [])}
        ok_diff = (c == 200 and fmap.get("server.py") == "changed"
                   and fmap.get("extra.py") == "added"
                   and r.get("counts", {}).get("changed") == 1)
        c, r = api("/api/versions/switch", {"vid": "v0001"}, token=bob_tok)
        ok_switch = (c == 200 and r.get("pre_snapshot") == "v0003")
        with open(os.path.join(root, "server.py"), encoding="utf-8") as f:
            back = f.read()
        kept = os.path.isfile(os.path.join(root, "extra.py"))
        marker = os.path.isfile(version_mod.RESTART_MARKER)
        _, r = api("/api/versions", token="t123")
        ok_list = (any(v["vid"] == "v0001" and v["active"]
                       for v in r.get("versions", []))
                   and "online" in r)
        chk("E2 版本链(建两版/diff/普通用户可切/滚前预快照/只覆盖不删/"
            "重启标记/清单含在线)",
            ok_v1 and ok_diff and ok_switch and "A=1" in back and kept
            and marker and version_mod.active_version() == "v0001" and ok_list,
            str(r)[:140])

        # ⑱ E2 删除权限与家法(用户403/纪元首版拒/进trash/号不回收/审计)
        c, _ = api("/api/versions", {"vid": v2}, method="DELETE",
                   token=bob_tok)
        ok_403 = c == 403
        c, r = api("/api/versions", {"vid": "v0001"}, method="DELETE",
                   token="t123")
        ok_first = c == 400 and "纪元" in str(r)
        c, r = api("/api/versions", {"vid": v2}, method="DELETE", token="t123")
        ok_del = c == 200 and os.path.isdir(os.path.join(
            version_mod.VERSIONS_DIR, ".trash", "v0002"))
        c, r = api("/api/versions", {"label": "再建"}, token="t123")
        ok_norecycle = (r.get("version") or {}).get("vid") == "v0004"
        _, r = api("/api/admin/audit?limit=80", token="t123")
        acts = [a["action"] for a in r.get("audit", [])]
        ok_audit = all(x in acts for x in ("version_create",
                                           "version_switch",
                                           "version_delete"))
        chk("E2 删除(用户403/纪元拒/进trash永不真删/号不回收/审计三动作)",
            ok_403 and ok_first and ok_del and ok_norecycle and ok_audit,
            str(acts)[:140])

        # ⑲ E2 写权限预检: 根不可写 → 干净报错, 且**不产生自动快照**
        #    (2026-10-19 线上实弹: root 属主的树, 3 次失败切换留下 3 个
        #     孤儿"切换前自动快照" —— 预检必须排在快照之前)
        n_before = len(version_mod.listing())
        saved_pw = version_mod._can_write_root
        version_mod._can_write_root = lambda: False
        ok_pw, r_pw = version_mod.switch_to("v0004", "probe")  # 活着且非激活
        version_mod._can_write_root = saved_pw
        chk("E2 预检(根不可写→干净拒+不产孤儿快照)",
            ok_pw is False and "不可写" in str(r_pw)
            and len(version_mod.listing()) == n_before, str(r_pw)[:100])

        # ㉑ E4 合并: 用户403; 逐文件挑边合成新版本; A/B 原封不动
        #    B 侧用 v0003(切换前预快照: server.py=A=2 且多 extra.py, 真差异)
        c, _ = api("/api/versions/merge", {"a": "v0001", "b": "v0003"},
                   token=bob_tok)
        ok_m403 = c == 403
        c, r = api("/api/versions/merge",
                   {"a": "v0001", "b": "v0003", "label": "合并版",
                    "picks": {"server.py": "a"}}, token="t123")
        mv = (r.get("version") or {}).get("vid")
        mdir = os.path.join(version_mod.VERSIONS_DIR, mv or "?", "files")
        ok_mv = False
        if mv:
            with open(os.path.join(mdir, "server.py"),
                      encoding="utf-8") as f:
                ok_mv = f.read() == "A=1\n"          # 挑了 A 侧(不是 B 的 A=2)
            ok_mv = ok_mv and os.path.isfile(
                os.path.join(mdir, "extra.py"))       # B 侧新文件并入
        with open(os.path.join(version_mod.VERSIONS_DIR, "v0003", "files",
                               "server.py"), encoding="utf-8") as f:
            b_intact = f.read().startswith("A=2")     # B 版原封不动
        _, r = api("/api/admin/audit?limit=100", token="t123")
        ok_maudit = "version_merge" in [a["action"]
                                        for a in r.get("audit", [])]
        chk("E4 合并(用户403/挑A侧+B侧新文件/A-B原封/审计)",
            ok_m403 and ok_mv and b_intact and ok_maudit,
            "mv=%s %s" % (mv, str(r)[:100]))

        # ㉕ E4.1 定型: 用户403; 旧版本+回收站整体归档(含 index.json 后悔药);
        #    当前工作区建成新纪元基线 v0001(编号跨纪元重启, 纪元内不回收照旧);
        #    单基线拒定型; 新基线是工作区快照(含 v0001 旧版没有的 extra.py)
        c, _ = api("/api/versions/finalize", {"label": "定型版"}, token=bob_tok)
        ok_f403 = c == 403
        c, r = api("/api/versions/finalize", {"label": "定型版"}, token="t123")
        fv = r.get("version") or {}
        ok_fin = c == 200 and fv.get("vid") == "v0001"
        farch = fv.get("archive") or ""
        vroot = version_mod.VERSIONS_DIR
        ok_farch = (os.path.isdir(farch)
                    and os.path.isdir(os.path.join(farch, "v0001"))
                    and os.path.isdir(os.path.join(farch, "v0003"))
                    and os.path.isdir(os.path.join(farch, "v0004"))
                    and os.path.isdir(os.path.join(farch, mv or "?"))
                    and os.path.isdir(os.path.join(farch, "trash", "v0002"))
                    and os.path.isfile(os.path.join(farch, "index.json")))
        ok_fgone = (not os.path.isdir(os.path.join(vroot, "v0003"))
                    and not os.path.isdir(os.path.join(vroot, ".trash")))
        ok_fact = version_mod.active_version() == "v0001"
        ok_fsnap = os.path.isfile(os.path.join(
            vroot, "v0001", "files", "extra.py"))   # 工作区快照非旧版拷贝
        f_list = version_mod.listing()
        ok_flist = (len(f_list) == 1 and f_list[0]["vid"] == "v0001"
                    and f_list[0]["active"])
        c, r = api("/api/versions/finalize", {"label": "x"}, token="t123")
        ok_fsingle = c == 400 and "只有一个基线版本" in str(r)
        c, r = api("/api/versions", {"label": "新纪元第一改"}, token="t123")
        ok_fno2 = (r.get("version") or {}).get("vid") == "v0002"
        _, r = api("/api/admin/audit?limit=150", token="t123")
        ok_faudit = "version_finalize" in [a["action"]
                                           for a in r.get("audit", [])]
        chk("E4.1 定型(用户403/旧版+回收站归档含index.json/清单只剩新基线/"
            "active指针/单基线拒定型/新纪元编号重启v0002/审计)",
            ok_f403 and ok_fin and ok_farch and ok_fgone and ok_fact
            and ok_fsnap and ok_flist and ok_fsingle and ok_fno2 and ok_faudit,
            "fin=%s arch=%s list=%s fv=%s" % (ok_fin, bool(farch),
                                              str(f_list)[:100], str(fv)[:100]))

        # ㉖ 媒体解码(2026-10-20): 纯 stdlib GIF→PNG(单帧/动图抽帧/像素正确) +
        #    stub ffmpeg/ffprobe 视频抽帧 + 缺 ffmpeg 的清晰错误.
        # 内嵌 2×2 GIF(手工构造: GCT 红绿蓝黑, 像素索引 [0,1,2,3]);
        # LZW 位流按规范"表满扩码宽": 第 3 个码起宽度 3→4(解码器与规范对齐).
        gif1 = bytes.fromhex(
            "47494638396102000200910000"
            "ff0000" "00ff00" "0000ff" "000000"   # GCT: 红绿蓝黑(24字符)
            "2c00000000020002000002"     # 图像描述符 + min code size
            "0344340500"                 # LZW: clear,0,1,2(w3)|3,5(w4)
            "3b")
        gif2 = bytes.fromhex(            # 第二帧像素 [3,2,1,0](黑蓝绿红)
            "47494638396102000200910000"
            "ff0000" "00ff00" "0000ff" "000000"
            "2c00000000020002000002"
            "0344340500"
            "2c00000000020002000002"
            "039c020500"
            "3b")
        ok_sniff = vision_mod._sniff_mime(gif1) == "image/gif"
        gif_path = os.path.join(_SBX, "t.gif")
        with open(gif_path, "wb") as f:
            f.write(gif1)
        refs1, meta1 = vision_mod.refs_from_path(gif_path, frames=1)
        ok_gifhead = (refs1 is not None and len(refs1) == 2
                      and refs1[0].get("mime") == "image/gif")   # 原图动图随行
        ok_px = False
        if ok_gifhead and refs1[1].get("mime") == "image/png":
            with open(refs1[1]["path"], "rb") as f:
                png = f.read()
            ok_px = png[:8] == b"\x89PNG\r\n\x1a\n"
            if ok_px:                    # 解 IDAT 校验像素(zlib 直解, filter 0)
                off, idat = 8, b""
                while off < len(png):
                    ln = struct.unpack(">I", png[off:off + 4])[0]
                    typ = png[off + 4:off + 8]
                    if typ == b"IDAT":
                        idat += png[off + 8:off + 8 + ln]
                    off += 12 + ln
                raw = zlib.decompress(idat)
                exp = (b"\x00\xff\x00\x00\xff\x00\xff\x00\xff"
                       + b"\x00\x00\x00\xff\xff\x00\x00\x00\xff")
                ok_px = raw == exp        # 红绿/蓝黑
        # api_messages: GIF 原图 ref 不喂模型(过滤, 落注记)
        msgs_vis = vision_mod.api_messages(
            [{"role": "user", "content": "看这个", "_images": [refs1[0]]}], True)
        flat = json.dumps(msgs_vis, ensure_ascii=False)
        ok_vfilt = "image_url" not in flat and "未附" in flat
        with open(gif_path, "wb") as f:
            f.write(gif2)
        refs2, meta2 = vision_mod.refs_from_path(gif_path, frames=2)
        ok_gif2 = (refs2 is not None and len(refs2) == 3
                   and refs2[0]["mime"] == "image/gif"
                   and all(r["mime"] == "image/png" for r in refs2[1:])
                   and "已抽 2 帧" in (meta2 or ""))
        # 视频: stub ffmpeg/ffprobe(测试缝改 FFMPEG/FFPROBE 模块属性)
        stub = os.path.join(_SBX, "stubbin")
        os.makedirs(stub, exist_ok=True)
        probe = os.path.join(stub, "ffprobe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho \'{"streams":[{"width":640,"height":360,'
                    '"r_frame_rate":"30/1"}],"format":{"duration":"6.0"}}\'\n')
        fm = os.path.join(stub, "ffmpeg")
        with open(fm, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\n'
                    'for last in "$@"; do :; done\n'
                    'D=$(dirname "$last")\n'
                    'printf "\\211PNG\\r\\n\\032\\nX" > "$D/f01.png"\n'
                    'printf "\\211PNG\\r\\n\\032\\nY" > "$D/f02.png"\n')
        os.chmod(probe, 0o755)
        os.chmod(fm, 0o755)
        vid = os.path.join(_SBX, "t.mp4")
        with open(vid, "wb") as f:
            f.write(b"not-a-real-video")
        old_fm, old_fp = vision_mod.FFMPEG, vision_mod.FFPROBE
        vision_mod.FFMPEG = fm
        vision_mod.FFPROBE = probe
        refs3, meta3 = vision_mod.refs_from_path(vid, frames=2)
        ok_vid = (refs3 is not None and len(refs3) == 2
                  and "视频 6.0s · 640x360 · 30.0fps · 已抽 2 帧" == meta3)
        vision_mod.FFMPEG = "no-such-ffmpeg-xyz"
        vision_mod.FFPROBE = "no-such-ffprobe-xyz"
        refs4, err4 = vision_mod.refs_from_path(vid, frames=2)
        ok_vmissing = refs4 is None and "ffmpeg" in (err4 or "")
        vision_mod.FFMPEG, vision_mod.FFPROBE = old_fm, old_fp
        chk("媒体解码(GIF嗅探/单帧PNG像素/GIF动图抽2帧/GIF原图随行不喂模型/"
            "stub视频抽2帧+元数据/缺ffmpeg清晰报错)",
            ok_sniff and ok_px and ok_gifhead and ok_vfilt and ok_gif2
            and ok_vid and ok_vmissing,
            "px=%s head=%s filt=%s gif2=%s vid=%s miss=%s meta3=%s"
            % (ok_px, ok_gifhead, ok_vfilt, ok_gif2, ok_vid, ok_vmissing,
               str(meta3)[:40]))

        # ㉗ 识图接线(2026-10-20): vision 位往返/目录透传/resolve 透传/家内档
        #    打位/look_image 点名端到端(吃剧本回复)/未勾视觉档报错含「能看图」
        c, r = api("/api/models", {"label": "自增视觉", "base_url": "http://x/v1",
                                   "key": "sk-v", "model": "v-model",
                                   "vision": True}, token="t123")
        ok_vsave = c == 200
        _, r = api("/api/health", token="t123")
        mvis = next((m for m in r.get("models", [])
                     if m.get("label") == "自增视觉"), None)
        ok_vcat = mvis is not None and mvis.get("vision") is True
        ok_vres = getattr(server.resolve_model("自增视觉"), "vision", False) is True
        c, _ = api("/api/models/vision", {"label": "自增视觉", "vision": False},
                   token="t123")
        _, r = api("/api/health", token="t123")
        mvis = next((m for m in r.get("models", [])
                     if m.get("label") == "自增视觉"), None)
        ok_voff = c == 200 and mvis is not None and mvis.get("vision") is False
        c, _ = api("/api/models/vision", {"label": "自增视觉", "vision": True},
                   token="t123")
        ok_von = c == 200
        saved_lp = provider_mod.load_providers      # fake 家只在 ㉒ 才重挂, 这里
        provider_mod.load_providers = lambda: dict(  # 自备(同 ㉒ 口径)
            {"fake": fake}, **old_customs())
        c, _ = api("/api/models/vision", {"provider": "fake", "model": "dm1",
                                          "vision": True}, token="t123")
        ok_vfam = c == 200
        ok_vfres = getattr(server.resolve_model("fake/dm1"), "vision", False) is True
        png1 = vision_mod._png_encode(1, 1, [b"\xff\x00\x00\xff"])
        pp = os.path.join(_SBX, "px.png")
        with open(pp, "wb") as f:
            f.write(png1)
        c, r = api("/api/sessions", {"model": "fake/fake-model"}, token="t123")
        sid3 = r.get("sid")
        fake.vision = True
        fake.script = [{"content": "图里看到美人鱼."}]   # 剧本喂模块级 fake ——
        fake.i = 0                                         # looker 的 copy 共享它;
        s3 = server.HUB.get(sid3)                          # (reset_script 设的是会话
        ret = s3.app.loop.ctx.look_image(pp, "图里有什么", "fake/fake-model", 1)
        ok_li = bool(ret) and ret.ok and "美人鱼" in (ret.content or "") \
            and bool(ret.images)            # 2026-10-20: 图随结论带出来
        ret2 = s3.app.loop.ctx.look_image(pp, "图里有什么", "", 1)
        ok_lirefuse = bool(ret2) and not ret2.ok \
            and "能看图" in (ret2.content or "")
        provider_mod.load_providers = saved_lp
        chk("识图接线(vision往返/目录透传/resolve透传/家内档打位/look_image"
            "点名E2E/未勾视觉档报错含能看图)",
            ok_vsave and ok_vcat and ok_vres and ok_voff and ok_von and ok_vfam
            and ok_vfres and ok_li and ok_lirefuse,
            "li=%s refuse=%s r=%s" % (ok_li, ok_lirefuse,
                                      str(ret2 and ret2.content)[:80]))

        # ㉘ 媒体端点(2026-10-20): 聊天流缩略图走 GET /api/media/<sha1>?token=
        #    (sha1 白名单正则, 无目录穿越面)
        mref = instr_mod.save_media(base64.b64encode(b"MEDIAPROBE").decode(
            "ascii"), "image/png")
        ok_m = mref is not None
        c, raw = api("/api/media/%s" % mref["sha1"], raw=True, token="t123")
        ok_m200 = c == 200 and raw == "MEDIAPROBE"   # api raw 返回解码后的 str
        c, _ = api("/api/media/%s" % mref["sha1"])
        ok_m401 = c == 401
        c, _ = api("/api/media/%s" % ("0" * 40), token="t123")
        ok_m404 = c == 404
        c, _ = api("/api/media/..%2F..%2Fetc", token="t123")
        ok_mtrav = c == 404
        chk("媒体端点(带token 200且字节一致/裸401/未知sha1 404/穿越404)",
            ok_m and ok_m200 and ok_m401 and ok_m404 and ok_mtrav,
            "m200=%s m401=%s m404=%s trav=%s"
            % (ok_m200, ok_m401, ok_m404, ok_mtrav))

        # ㉙ 忆库操作(2026-10-20): 遗忘/恢复两区挪动 + 真删
        memory_mod.write_card("待删卡", "内容")
        c, r = api("/api/memory/zone", {"title": "待删卡", "to": "forget"},
                   token="t123")
        ok_zf = c == 200
        _, r = api("/api/memory", token="t123")
        ok_zlist = any(x["title"] == "待删卡" and x["zone"] == "forget"
                       for x in r.get("cards", []))
        c, _ = api("/api/memory/zone", {"title": "待删卡", "to": "archive"},
                   token="t123")
        ok_zb = c == 200
        c, _ = api("/api/memory/card", {"title": "待删卡"}, method="DELETE",
                   token="t123")
        ok_del = c == 200
        _, r = api("/api/memory", token="t123")
        ok_gone = all(x["title"] != "待删卡" for x in r.get("cards", []))
        c, _ = api("/api/memory/card", {"title": "待删卡"}, method="DELETE",
                   token="t123")
        ok_d404 = c == 404
        chk("忆库操作(遗忘入区/恢复回区/真删消失/再删404)",
            ok_zf and ok_zlist and ok_zb and ok_del and ok_gone and ok_d404,
            "zf=%s zlist=%s zb=%s del=%s gone=%s 404=%s"
            % (ok_zf, ok_zlist, ok_zb, ok_del, ok_gone, ok_d404))

        # ㉒ E5 持久会话: drop 模拟重启 → 归档可见+磁盘回放+越权 403×2
        #     + resume 同 sid 续聊(transcript 继续增长)
        #     (E5.1 起 DELETE=真删, 模拟重启改走 HUB.drop 不 purge)
        sid_file = os.path.join(session_mod.SESSIONS_DIR, sid + ".jsonl")
        ok_file = os.path.isfile(sid_file)
        server.HUB.drop(sid)
        time.sleep(0.5)
        _, r = api("/api/sessions", token="t123")
        arch = next((x for x in r.get("sessions", [])
                     if x["sid"] == sid), None)
        ok_arch = arch is not None and arch.get("archived")
        _, r = api("/api/sessions/%s/messages" % sid, token="t123")
        ok_play = any("README" in (m.get("content") or "")
                      for m in r.get("messages", []))
        c, _ = api("/api/sessions/%s/messages" % sid, token=bob_tok)
        ok_403a = c == 403                # 老档无归属 → 仅超管可见
        c, _ = api("/api/sessions", {"model": "fake/fake-model",
                                     "resume": sid}, token=bob_tok)
        ok_403r = c == 403
        provider_mod.load_providers = lambda: dict(   # ⑯ 撤过假链, 这里挂回
            {"fake": fake}, **old_customs())
        c, r = api("/api/sessions", {"model": "fake/fake-model",
                                     "resume": sid}, token="t123")
        r_resume_err = r
        ok_resume = c == 200 and r.get("sid") == sid and r.get("resumed")
        sse2 = None
        if ok_resume:
            reset_script(sid, [{"content": "收到, 接着说."}])
            global _SSE_TOKEN
            _SSE_TOKEN = "t123"
            sse2 = SSE(sid)
            api("/api/sessions/%s/send" % sid, {"text": "再补一句"},
                token="t123")
            fin = sse2.wait(lambda ev: ev and ev.get("type") == "turn_end")
            ok_resume = ok_resume and bool(fin)
        with open(sid_file, encoding="utf-8") as f:
            grew = f.read().count("再补一句") >= 1
        if sse2 is not None:
            sse2.close()
        chk("E5 持久会话(drop后归档可见/磁盘回放/越权403×2/resume同sid续聊)",
            ok_file and ok_arch and ok_play and ok_403a and ok_403r
            and ok_resume and grew,
            "file=%s arch=%s play=%s 403a=%s 403r=%s resume=%s grew=%s r=%s"
            % (ok_file, bool(arch), ok_play, ok_403a, ok_403r,
               ok_resume, grew, str(r_resume_err)[:120]))

        # ㉓ E5.1 删除: 归档真删(越权403/文件消失/清单消失/未知404)+活删防复活
        server.HUB.drop(sid)                    # 活会话先归位成归档(不 purge)
        time.sleep(0.5)
        c, _ = api("/api/sessions/%s" % sid, {}, method="DELETE", token=bob_tok)
        ok_d403 = c == 403
        trace_file = os.path.join(instr_mod.TRACE_DIR, sid + ".jsonl")
        c, r = api("/api/sessions/%s" % sid, {}, method="DELETE", token="t123")
        ok_del = c == 200 and r.get("ok")
        ok_gone = not (os.path.isfile(sid_file)
                       or os.path.isfile(trace_file))
        _, r = api("/api/sessions", token="t123")
        ok_nolist = all(x["sid"] != sid for x in r.get("sessions", []))
        c, _ = api("/api/sessions/s0bad0bad_0bad0", {}, method="DELETE",
                   token="t123")
        ok_d404 = c == 404
        c, r = api("/api/sessions", {"model": "fake/fake-model"}, token="t123")
        sid2 = r.get("sid")
        f2 = os.path.join(session_mod.SESSIONS_DIR, sid2 + ".jsonl")
        ok_f2 = bool(sid2) and os.path.isfile(f2)
        fin3 = None
        if ok_f2:
            reset_script(sid2, [{"content": "收到."}])
            _SSE_TOKEN = "t123"
            sse3 = SSE(sid2)
            api("/api/sessions/%s/send" % sid2, {"text": "你好"}, token="t123")
            fin3 = sse3.wait(lambda ev: ev and ev.get("type") == "turn_end")
            sse3.close()
        c, _ = api("/api/sessions/%s" % sid2, {}, method="DELETE", token="t123")
        time.sleep(0.5)
        ok_livegone = ok_f2 and bool(fin3) and c == 200 \
            and not os.path.isfile(f2) and server.HUB.get(sid2) is None
        chk("E5.1 删除(归档越权403/真删文件+清单消失/未知404/活删防复活)",
            ok_d403 and ok_del and ok_gone and ok_nolist and ok_d404
            and ok_livegone,
            "403=%s del=%s gone=%s nolist=%s 404=%s live=%s"
            % (ok_d403, ok_del, ok_gone, ok_nolist, ok_d404, ok_livegone))
        provider_mod.PROVIDERS_CHAIN = old_chain
        provider_mod.KEY_STORE = old_keystore
        provider_mod.fetch_models = old_fetch
        server.TOKEN = ""
    finally:
        if sse is not None:
            sse.close()
        if sid:
            api("/api/sessions/%s" % sid, {}, method="DELETE")
        httpd.shutdown()
        provider_mod.load_providers = old_provs
        provider_mod.PROVIDERS_CHAIN = old_chain
        provider_mod.KEY_STORE = old_keystore
        provider_mod.fetch_models = old_fetch
        for mod, a, v in saved:
            setattr(mod, a, v)
        instr_mod.raw_store_sink = old_raw_sink

    n_fail = 0
    for name, ok, detail in checks:
        print("%-4s %s %s" % ("PASS" if ok else "FAIL", name,
                              detail if not ok else ""))
        if not ok:
            n_fail += 1
    print("WEBCHECK %s (%d 项)" % ("ALL PASS" if not n_fail else
                                   "FAILED: %d 红" % n_fail, len(checks)))
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

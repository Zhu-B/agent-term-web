# -*- coding: utf-8 -*-
"""selfcheck.py — Web 后端内置自检(不联网, 三件套同款精神).

沙箱纪律: keys/models/settings/记忆/账本/定时/会话/trace 全部指临时目录,
绝不碰服务器上的真实配置; provider 用剧本 FakeProvider(照 _verify 先例).
链路: health → 加自增档(掩码) → 建会话 → SSE 收全程事件序列 → perm 三键卡
真问真答 → ask_user 卡真问真答 → 中断 → 静态托管 → token 鉴权.

用法: python server.py --check
"""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# E1.3: AT_DATA 一根线把全部服务端状态指进沙箱(server 在 import core 前读它)
_SBX = tempfile.mkdtemp(prefix="at_web_check_")
os.environ["AT_DATA"] = _SBX
import server                      # noqa: E402  (顺带把 core/ 挂上 sys.path)
import provider as provider_mod
import session as session_mod
import instr as instr_mod
import memory as memory_mod
import webusers
from provider import ChatResult

BASE_URL = None                    # build_server 分到的地址


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
        req = urllib.request.Request(
            "%s/api/sessions/%s/events?since=0" % (BASE_URL, sid))
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

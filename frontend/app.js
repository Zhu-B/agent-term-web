/* agent-term web 前端 —— 纯原生 JS, 零依赖零外链.
 * 事件投影渲染口径与后端 project_web 一一对应; SSE 断线用 since=seq 续传. */
"use strict";

/* ---------------- 配置(localStorage) ---------------- */
const CFG = {
  base: localStorage.getItem("at_base") || "",
  token: localStorage.getItem("at_token") || "",
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function apiBase() {
  // 留空 = 同源(后端挂前端部署); http(s) 开头 = 跨源
  return CFG.base ? CFG.base.replace(/\/+$/, "") : "";
}
async function api(path, body, method) {
  const opt = { method: method || (body !== undefined ? "POST" : "GET"),
                headers: { "Content-Type": "application/json" } };
  if (CFG.token) opt.headers["Authorization"] = "Bearer " + CFG.token;
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(apiBase() + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.err || ("HTTP " + r.status));
  return j;
}

/* ---------------- 全局状态 ---------------- */
const S = {
  sid: null, es: null, lastSeq: 0, streaming: null, busy: false,
  models: [], model: localStorage.getItem("at_model") || "",
};

/* ---------------- 聊天流渲染 ---------------- */
const chat = $("chat");
function scrollDown() { chat.scrollTop = chat.scrollHeight; }
function addLine(text, warn) {
  const d = document.createElement("div");
  d.className = "dim-line" + (warn ? " warn" : "");
  d.textContent = text;
  chat.appendChild(d); scrollDown(); return d;
}
function bubble(cls) {
  const d = document.createElement("div");
  d.className = cls; chat.appendChild(d); return d;
}

function render(ev) {
  const t = ev.type;
  if (ev.seq) { S.lastSeq = Math.max(S.lastSeq, ev.seq); }
  switch (t) {
    case "turn_start":
      bubble("bubble-user").textContent = ev.text || "";
      S.busy = true; setBusy(); break;
    case "llm_chunk": {
      if (!ev.text) break;
      if (!S.streaming) S.streaming = bubble("bubble-asst");
      S.streaming.textContent += ev.text;
      scrollDown();
      break;
    }
    case "llm_response": {
      if (S.streaming) {                 // 流式泡收口(整段覆盖, 防残缺)
        S.streaming.textContent = ev.text || "";
        S.streaming = null; break;
      }
      if (ev.text) bubble("bubble-asst").textContent = ev.text;
      break;
    }
    case "tool_call": {
      const row = document.createElement("div");
      row.className = "tool-row";
      row.innerHTML =
        '<div class="t-head"><span class="mark">⚒</span>' + esc(ev.name) +
        '<span class="t-args-inline"></span></div>' +
        '<div class="t-args"></div>';
      row.querySelector(".t-args").textContent = ev.args || "";
      row.querySelector(".t-head").onclick = () =>
        row.classList.toggle("open");
      row.dataset.name = ev.name; chat.appendChild(row); scrollDown();
      break;
    }
    case "tool_result": {
      const rows = [...chat.querySelectorAll(".tool-row")];
      const row = rows.reverse().find((r) => !r.dataset.res);
      if (row) {
        row.dataset.res = "1";
        row.classList.add(ev.ok ? "ok" : "bad");
        row.querySelector(".mark").textContent = ev.ok ? "✔" : "✘";
        const ms = document.createElement("span");
        ms.className = "t-ms"; ms.textContent = (ev.ms || 0) + "ms";
        row.querySelector(".t-head").appendChild(ms);
        const body = document.createElement("div");
        body.className = "t-body"; body.textContent = ev.content || "";
        row.appendChild(body);
      } else {
        addLine((ev.ok ? "✔ " : "✘ ") + (ev.content || "").slice(0, 300));
      }
      break;
    }
    case "perm_decision":
      addLine("· " + ev.name + " → " +
        (ev.decision === "deny" ? "拒绝" : ev.decision === "always"
          ? "本会话总允许" : "允许"));
      break;
    case "sub_req": addLine("⤷[" + ev.label + "] 起跑 " + (ev.model || "")); break;
    case "sub_tool_call":
      addLine("⤷[" + ev.label + "] " + ev.name); break;
    case "sub_tool_result":
      addLine("⤷[" + ev.label + "] " + (ev.ok ? "✔" : "✘") +
        " " + (ev.content || "").slice(0, 120)); break;
    case "sub_done":
      addLine("⤷[" + ev.label + "] 分身收工(" + (ev.iters || "?") + " 轮)"); break;
    case "todo_update": {
      const box = document.createElement("div");
      box.className = "todo-box";
      box.innerHTML = (ev.items || []).map((it) =>
        '<div class="' + (it.status === "done" ? "done" : "") + '">' +
        (it.status === "done" ? "☑ " : "☐ ") + esc(it.subject || "") +
        "</div>").join("");
      chat.appendChild(box); scrollDown(); break;
    }
    case "memory_write": addLine("· 记忆卡: " + ev.title); break;
    case "memory_digest":
      addLine("· 轮末记忆整理" + (ev.n ? ": " + ev.n + " 张" : "")); break;
    case "bg_done":
      addLine("· ⚙ 后台任务 " + ev.id + " 结束(rc=" + ev.rc + ", " +
        ev.seconds + "s): " + (ev.cmd_head || "")); break;
    case "auto_compact": addLine("· 上下文水位超线, 已自动压缩"); break;
    case "error": addLine("✘ " + (ev.where || "") + " " + (ev.repr || ""), 1); break;
    case "note": addLine(ev.text || ""); break;
    case "turn_end": {
      S.busy = false; setBusy();
      const u = ev.usage || {};
      addLine("— " + (ev.iters || "?") + " 轮 · in " +
        (u.prompt_tokens || 0) + "/out " + (u.completion_tokens || 0) +
        " tokens" + (ev.aborted ? " · 已中断" : ""));
      break;
    }
    case "perm_ask": permCard(ev); break;
    case "ask_user": askCard(ev); break;
    case "ask_done": {
      const card = document.querySelector('[data-ask="' + ev.id + '"]');
      if (card) {
        card.classList.add("done");
        const p = document.createElement("div");
        p.className = "picked";
        p.textContent = "已答: " + JSON.stringify(ev.answer);
        card.appendChild(p);
      }
      break;
    }
  }
}

/* ---- 两张问卡 ---- */
function permCard(ev) {
  const c = document.createElement("div");
  c.className = "ask-card"; c.dataset.ask = ev.id;
  c.innerHTML = '<div class="q">⛔ 要放行这次工具调用吗?</div>' +
    '<div class="meta">' + esc(ev.name) + " · 风险 " + esc(ev.risk || "?") +
    "<br>" + esc(ev.args || "") + "</div>" +
    '<div class="opts"></div>';
  const opts = c.querySelector(".opts");
  [["deny", "拒绝"], ["allow", "允许"], ["always", "本会话总允许"]].forEach(
    ([v, lab]) => {
      const b = document.createElement("button");
      b.textContent = lab; if (v === "deny") b.className = "danger";
      b.onclick = () => answer(c, ev.id, { answer: v });
      opts.appendChild(b);
    });
  chat.appendChild(c); scrollDown();
}
function askCard(ev) {
  const c = document.createElement("div");
  c.className = "ask-card"; c.dataset.ask = ev.id;
  c.innerHTML = '<div class="q">' + esc(ev.question || "") + "</div>" +
    '<div class="meta">' + esc(ev.header || "") + "</div>" +
    '<div class="opts"></div>' +
    '<div class="opts" style="margin-top:7px">' +
    '<input type="text" placeholder="或自填…">' +
    '<button data-k="skip">跳过</button></div>';
  const o1 = c.querySelectorAll(".opts")[0];
  (ev.options || []).forEach((lab) => {
    const b = document.createElement("button");
    b.textContent = lab;
    b.onclick = () => answer(c, ev.id, { label: lab });
    o1.appendChild(b);
  });
  const o2 = c.querySelectorAll(".opts")[1];
  o2.querySelector("button").onclick = () =>
    answer(c, ev.id, { skipped: true });
  const inp = o2.querySelector("input");
  inp.onkeydown = (e) => { if (e.key === "Enter")
    answer(c, ev.id, { custom: inp.value }); };
  chat.appendChild(c); scrollDown(); inp.focus();
}
async function answer(card, id, body) {
  try {
    await api("/api/sessions/" + S.sid + "/ask/" + id + "/answer", body);
  } catch (e) { addLine("✘ 答题失败: " + e.message, 1); }
}

/* ---------------- SSE ---------------- */
function openEvents() {
  if (S.es) { S.es.close(); S.es = null; }
  const url = apiBase() + "/api/sessions/" + S.sid + "/events?since=" +
    S.lastSeq + (CFG.token ? "&token=" + encodeURIComponent(CFG.token) : "");
  const es = new EventSource(url);
  S.es = es;
  es.onmessage = (m) => {
    try { render(JSON.parse(m.data)); } catch (e) { /* 坏行丢弃 */ }
  };
  es.onerror = () => {           // 手动续传: 带 lastSeq 重开(EventSource
    es.close();                  // 自动重连会重放全量造成重复渲染)
    setTimeout(() => { if (S.sid) openEvents(); }, 1500);
  };
}

/* ---------------- 会话与模型 ---------------- */
async function refreshSessions() {
  const r = await api("/api/sessions");
  const box = $("sessions"); box.innerHTML = "";
  (r.sessions || []).forEach((s) => {
    const d = document.createElement("div");
    d.className = "sess" + (s.sid === S.sid ? " cur" : "");
    d.innerHTML = (s.busy ? '<span class="busy-dot">● </span>' : "") +
      (s.archived ? "🗂 " : "") +
      "<span class='s-lab'>" + esc(s.label) + "</span>" +
      "<span class='s-x' title='" +
      (s.archived ? "删除历史会话" : "关闭会话") + "'>✕</span>" +
      "<br>" +
      "<span style='color:var(--dim);font-size:11px'>" + s.sid +
      (s.archived ? " · 历史" + (s.msgs ? "·" + s.msgs + "条" : "") : "") +
      (s.owner && s.owner !== (S.me || {}).uid ? " ·他人" : "") + "</span>";
    d.onclick = () => (s.archived ? openArchived(s.sid)
                                  : openSession(s.sid));
    d.querySelector(".s-x").onclick = async (e) => {
      e.stopPropagation();
      const msg = s.archived
        ? "删除历史会话 " + s.sid + "? (服务器端真删磁盘档, 不可恢复)"
        : "关闭并删除会话 " + s.sid + "? (服务器侧收尾销毁)";
      if (!confirm(msg)) return;
      try {
        await api("/api/sessions/" + s.sid, undefined, "DELETE");
        if (S.sid === s.sid) {       // 删的是当前会话: 清空聊天区
          if (S.es) { S.es.close(); S.es = null; }
          S.sid = null;
          $("arch-bar").hidden = true;
          chat.innerHTML = '<div class="dim-line">会话已删除, 新建一个开始</div>';
        }
        refreshSessions();
      } catch (err) { alert("删除失败: " + err.message); }
    };
    box.appendChild(d);
  });
}
async function newSession() {
  let model = $("model-sel").value;
  if (!model) {
    alert("还没有可调用的模型 —— 先去「模型」页给某家贴 key 或加自增档, 拉清单选一个。");
    showView("models");
    return;
  }
  try {
    const r = await api("/api/sessions", { model });
    localStorage.setItem("at_model", model);
    await refreshSessions();
    openSession(r.sid);
  } catch (e) { addLine("✘ 建会话失败: " + e.message, 1); }
}
async function openSession(sid) {
  showView("chat");                  // 在别的页点会话: 先切回聊天页再打开
  S.sid = sid; S.lastSeq = 0; S.streaming = null; S.archived = false;
  $("arch-bar").hidden = true;
  $("input").disabled = false;
  chat.innerHTML = ""; setBusy(false);
  renderConn("已连接", "ok");
  try {
    const r = await api("/api/sessions/" + sid + "/messages");
    (r.messages || []).forEach((m) => {
      bubble(m.role === "user" ? "bubble-user" : "bubble-asst")
        .textContent = m.content;
    });
  } catch (e) { /* 历史兜底失败不挡 SSE */ }
  openEvents();
  refreshSessions();
}
/* ---- E5 历史会话: 只读回放 + 一键续聊(同 sid 接着聊) ---- */
async function openArchived(sid) {
  showView("chat");
  S.sid = sid; S.lastSeq = 0; S.streaming = null; S.archived = true;
  if (S.es) { S.es.close(); S.es = null; }
  chat.innerHTML = "";
  setBusy(true);                     // 只读: 锁输入
  $("input").disabled = true;
  $("arch-bar").hidden = false;
  $("arch-note").textContent = "历史会话(只读) · " + sid;
  renderConn("已连接", "ok");
  try {
    const r = await api("/api/sessions/" + sid + "/messages");
    (r.messages || []).forEach((m) => {
      bubble(m.role === "user" ? "bubble-user" : "bubble-asst")
        .textContent = m.content;
    });
    chat.scrollTop = chat.scrollHeight;
  } catch (e) { addLine("✘ 读档失败: " + e.message, 1); }
}
$("arch-resume").onclick = async () => {
  const model = $("model-sel").value;
  if (!model) { alert("先在侧栏选一个模型, 再继续会话"); return; }
  try {
    const r = await api("/api/sessions", { model, resume: S.sid });
    openSession(r.sid);              // 同一 sid, 这回是活的
  } catch (e) { alert("续会话失败: " + e.message); }
};
async function loadModels() {
  const r = await api("/api/health");
  S.models = r.models || [];
  const sel = $("model-sel");
  sel.innerHTML = "";
  const okModels = S.models.filter((m) => m.available);
  okModels.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.label;
    o.textContent = m.label + (m.kind === "demo" ? " (演示)" : "");
    sel.appendChild(o);
  });
  if (!okModels.length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "(无可调模型 → 去「模型」页配 key)";
    sel.appendChild(o);
  }
  if (S.model && okModels.some((m) => m.label === S.model))
    sel.value = S.model;
  renderModelsList();
  return r;
}
function renderModelsList() {
  const box = $("models-list");
  box.innerHTML = "";
  const ok = S.models.filter((m) => m.available);
  if (!ok.length) {
    box.innerHTML = '<p class="dim">还没有可调用的模型 —— 上面贴 key 或加自增档</p>';
    return;
  }
  ok.forEach((m) => {
    const d = document.createElement("div");
    d.className = "mrow";
    d.innerHTML = '<span class="lab">' + esc(m.label) + "</span>" +
      '<span class="kind">' + esc(m.kind) + "</span>" +
      "<span>" + esc(m.model || "") + "</span>" +
      '<span class="mask">' + esc(m.key_mask || "") + "</span>" +
      '<button class="m-del">✕ 删</button>';
    d.querySelector(".m-del").onclick = async () => {
      try {
        await api("/api/models", { label: m.label,
                                   provider: m.kind === "family"
                                     ? m.label.split("/")[0] : "custom",
                                   model: m.model }, "DELETE");
        await loadModels();
      } catch (e) { alert("删除失败: " + e.message); }
    };
    box.appendChild(d);
  });
}

/* ---------------- 输入 ---------------- */
function setBusy(busy) {
  if (busy !== undefined) S.busy = busy;
  $("btn-stop").disabled = !S.busy || !!S.archived;
  $("btn-stop").textContent = "中断";
  $("btn-send").disabled = S.busy;
  $("btn-send").textContent = S.archived ? "只读" :
    (S.busy ? "跑着…" : "发送");
}
async function send() {
  const ta = $("input");
  const text = ta.value.trim();
  if (!text || !S.sid || S.archived) return;
  ta.value = "";
  try {
    await api("/api/sessions/" + S.sid + "/send", { text });
  } catch (e) { addLine("✘ 发送失败: " + e.message, 1); }
}
$("btn-send").onclick = send;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("btn-stop").onclick = () => {
  if (!S.sid) return;
  $("btn-stop").textContent = "中断中…";     // 先给反馈; turn_end 时 setBusy 复位
  api("/api/sessions/" + S.sid + "/interrupt", {}).catch((e) =>
    addLine("✘ 中断请求失败: " + e.message, 1));
};
$("btn-new").onclick = newSession;

/* ---------------- 页面切换(会话/模型/忆库/技能/账本/版本) ---------------- */
const VIEWS = ["chat", "models", "memlib", "skills", "ledger", "versions",
               "admin"];
function showView(name) {
  VIEWS.forEach((v) => { $(v + "-view").hidden = v !== name; });
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.classList.toggle("cur", b.dataset.view === name));
  if (name === "memlib") loadMemlib();
  if (name === "skills") loadSkills();
  if (name === "ledger") loadLedger();
  if (name === "versions") loadVersions();
  if (name === "admin") loadAdmin();
  if (name === "models") { loadModels().then(loadProviders); }
}

/* ---- E1.2 官方家: 卡片 + 存 key + 拉清单 + 点 chip 加入 ---- */
async function loadProviders() {
  let r;
  try { r = await api("/api/providers"); }
  catch (e) { return; }
  const box = $("providers-list");
  box.innerHTML = "";
  const added = new Set((S.models || []).map((m) => m.label));
  (r.providers || []).forEach((pr) => {
    const d = document.createElement("div");
    d.className = "prov-card card";
    const st = pr.available
      ? "可调" + (pr.key_src === "私有" ? "(私有key)" : "")
      : "缺 key";
    d.innerHTML =
      '<div class="p-head"><b>' + esc(pr.name) + '</b>' +
      '<span class="p-fam">' + esc(pr.family || "") + "</span>" +
      '<span class="p-st ' + (pr.available ? "on" : "") + '">' + st + "</span>" +
      '<span class="p-url">' +
      esc((pr.base_url || "").replace(/^https?:\/\//, "")) + "</span></div>" +
      '<div class="p-row">' +
      '<input type="password" placeholder="API key' +
      (pr.key_mask ? "(现: " + pr.key_mask + ")" : "") + '">' +
      '<button class="p-save">存 key</button>' +
      '<button class="p-fetch">拉清单</button></div>' +
      '<div class="p-chips"><span class="chip-hint">拉清单后在这里挑型号</span></div>';
    const input = d.querySelector("input");
    const chips = d.querySelector(".p-chips");
    d.querySelector(".p-save").onclick = async () => {
      try {
        const rr = await api("/api/providers/key",
                            { name: pr.name, key: input.value.trim() });
        d.querySelector(".p-st").textContent =
          "已存(" + rr.key_src + " " + rr.key_mask + ")";
        input.value = "";
        loadModels().then(loadProviders);
      } catch (e) { alert(e.message); }
    };
    d.querySelector(".p-fetch").onclick = async () => {
      chips.innerHTML = '<span class="chip-hint">拉取中…</span>';
      try {
        const rr = await api("/api/providers/fetch", { name: pr.name });
        chips.innerHTML = "";
        if (!rr.ok) {
          chips.innerHTML = '<span class="chip-hint">' + esc(rr.err) +
            "(先存 key 再拉)</span>";
          return;
        }
        if (!(rr.models || []).length) {
          chips.innerHTML = '<span class="chip-hint">清单是空的</span>';
          return;
        }
        (rr.models).forEach((m) => {
          const c = document.createElement("span");
          const lab = pr.name + "/" + m;
          c.className = "chip" + (added.has(lab) ? " cur" : "");
          c.textContent = m + (added.has(lab) ? " ✓" : "");
          c.onclick = async () => {
            if (c.classList.contains("cur")) return;
            try {
              await api("/api/models", { provider: pr.name, model: m });
              c.classList.add("cur");
              c.textContent = m + " ✓";
              await loadModels();
            } catch (e) { alert(e.message); }
          };
          chips.appendChild(c);
        });
      } catch (e) {
        chips.innerHTML = '<span class="chip-hint">' + esc(e.message) + "</span>";
      }
    };
    box.appendChild(d);
  });
}
document.querySelectorAll(".nav-btn").forEach((b) =>
  (b.onclick = () => showView(b.dataset.view)));

/* ---- 忆库: 两区卡列 + 点开读 + 注入当前会话 ---- */
async function loadMemlib() {
  const r = await api("/api/memory");
  const reads = JSON.parse(localStorage.getItem("at_reads") || "{}");
  const mk = (zone) => {
    const box = $(zone === "archive" ? "mem-archive" : "mem-forget");
    box.innerHTML = "";
    (r.cards || []).filter((c) => c.zone === zone).forEach((c) => {
      const d = document.createElement("div");
      d.className = "mem-card" + (zone === "forget" ? " forget" : "")
        + (reads[c.title] ? " read" : "");
      d.innerHTML = '<div class="t">' + esc(c.title) + '</div>' +
        '<div class="h">' + esc(c.head || "") + "</div>";
      d.onclick = () => openCard(c, d);
      box.appendChild(d);
    });
    if (!box.children.length) box.innerHTML = '<p class="dim">空</p>';
  };
  mk("archive");
  mk("forget");
}
async function openCard(c, el) {
  try {
    const r = await api("/api/memory/card?title=" +
      encodeURIComponent(c.title));
    const box = $("mem-reader");
    box.innerHTML = "";
    const h = document.createElement("h4");
    h.textContent = c.title;
    const ops = document.createElement("div");
    ops.className = "ops";
    const inj = document.createElement("button");
    inj.className = "primary";
    inj.textContent = "注入到当前会话";
    inj.disabled = !S.sid;
    inj.onclick = async () => {
      try {
        await api("/api/sessions/" + S.sid + "/memory", { title: c.title });
        inj.textContent = "已注入 ✓";
      } catch (e) { inj.textContent = "失败: " + e.message; }
    };
    ops.appendChild(inj);
    const body = document.createElement("div");
    body.textContent = r.content || "";
    box.append(h, ops, body);
    const reads = JSON.parse(localStorage.getItem("at_reads") || "{}");
    reads[c.title] = 1;
    localStorage.setItem("at_reads", JSON.stringify(reads));
    if (el) el.classList.add("read");
  } catch (e) { $("mem-reader").innerHTML =
    '<p class="dim">读卡失败: ' + esc(e.message) + "</p>"; }
}

/* ---- 技能: 清单 + 点开读全文 ---- */
async function loadSkills() {
  const r = await api("/api/skills");
  const box = $("skills-list");
  box.innerHTML = "";
  (r.skills || []).forEach((s) => {
    const d = document.createElement("div");
    d.className = "mem-card" + (s.shadowed ? " forget" : "");
    d.innerHTML = '<div class="t">' + esc(s.name) +
      (s.source === "user" ? " ·个人" : "") +
      (s.shadowed ? " ·被个人层覆盖" : "") + '</div>' +
      '<div class="h">' + esc(s.description || "") + "</div>";
    d.onclick = async () => {
      try {
        const rr = await api("/api/skill?name=" +
          encodeURIComponent(s.name));
        $("skill-reader").innerHTML = "<h4>" + esc(s.name) + "</h4><div>" +
          esc(rr.body || "").replace(/\n/g, "<br>") + "</div>";
      } catch (e) { $("skill-reader").innerHTML =
        '<p class="dim">读取失败: ' + esc(e.message) + "</p>"; }
    };
    box.appendChild(d);
  });
  if (!box.children.length) box.innerHTML = '<p class="dim">没有技能</p>';
}

/* ---- 账本: 聚总 chips + 明细表 ---- */
async function loadLedger() {
  const r = await api("/api/ledger?limit=150");
  const agg = $("ledger-agg");
  agg.innerHTML = "";
  (r.aggregate || []).forEach((g) => {
    const d = document.createElement("div");
    d.className = "agg-chip";
    d.innerHTML = "<b>" + esc(g.model) + "</b> · " + g.calls + " 笔 / 成功 " +
      g.ok + " / " + ((g.tokens || {}).total_tokens || 0) + " tok" +
      (g.avg_score != null
        ? ' · <span class="sc">评分 ' + g.avg_score + "</span>" : "");
    agg.appendChild(d);
  });
  if (!(r.aggregate || []).length)
    agg.innerHTML = '<p class="dim">还没有账(派分身/识图会自动记)</p>';
  const tb = document.querySelector("#ledger-table tbody");
  tb.innerHTML = "";
  (r.entries || []).forEach((e) => {
    const tr = document.createElement("tr");
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleString("zh-CN",
      { hour12: false }) : "";
    tr.innerHTML =
      "<td>" + esc(ts) + "</td>" +
      "<td>" + esc(e.sub || "") + "</td>" +
      "<td>" + esc(e.model || "") + "</td>" +
      "<td>" + esc((e.task || "").slice(0, 50)) + "</td>" +
      '<td class="' + (e.ok ? "ok-t" : "ok-f") + '">' +
      esc((e.result_head || "").slice(0, 60)) + "</td>" +
      "<td>" + ((e.tokens || {}).total_tokens || 0) + "</td>" +
      "<td>" + (e.wall_s || "") + "s</td>";
    tb.appendChild(tr);
  });
}
$("m-save").onclick = async () => {
  try {
    const r = await api("/api/models", {
      label: $("m-label").value.trim(), base_url: $("m-base").value.trim(),
      key: $("m-key").value.trim(), model: $("m-model").value.trim() });
    $("m-msg").textContent = "已保存 " + r.label + " " + (r.key_mask || "");
    await loadModels();
  } catch (e) { $("m-msg").textContent = "失败: " + e.message; }
};

/* ---- E2 版本: 清单/建/切换(全局生效)/diff/超管删除 ---- */
async function loadVersions() {
  let r;
  try { r = await api("/api/versions"); }
  catch (e) { $("versions-list").innerHTML =
    '<p class="dim">读取失败: ' + esc(e.message) + "</p>"; return; }
  S.versions = r;
  const box = $("versions-list");
  box.innerHTML = "";
  const on = r.online || {count: 0, users: []};
  const onNote = "当前在线 " + (on.count || 0) + " 人" +
    (on.busy_sessions ? " · " + on.busy_sessions + " 个回合在跑" : "");
  (r.versions || []).forEach((v) => {
    const d = document.createElement("div");
    d.className = "vrow card";
    const t = v.created_at ? new Date(v.created_at * 1000).toLocaleString(
      "zh-CN", {hour12: false}) : "";
    d.innerHTML =
      '<div class="v-head"><b>' + esc(v.vid) + '</b>' +
      '<span class="v-lab">' + esc(v.label || "(未命名)") + "</span>" +
      (v.active ? '<span class="v-cur">当前</span>' : "") +
      '<span class="v-meta">' + esc(v.created_by || "") + " · " + esc(t) +
      " · " + (v.file_count || 0) + " 文件 / " +
      Math.round((v.total_bytes || 0) / 1024) + " KB</span></div>" +
      (v.note ? '<div class="v-note">' + esc(v.note) + "</div>" : "") +
      '<div class="ops"></div>';
    const ops = d.querySelector(".ops");
    if (!v.active) {
      const sw = document.createElement("button");
      sw.className = "danger";
      sw.textContent = "切换到此版本";
      sw.onclick = async () => {
        if (!confirm("切换到 " + v.vid + " ?\n\n" + onNote +
            "\n全局生效: 所有人立即切到该版本, 服务闪断数秒。\n" +
            "(切换前会自动快照当前状态, 可随时切回)")) return;
        sw.disabled = true;
        sw.textContent = "切换中…";
        try {
          const rr = await api("/api/versions/switch", {vid: v.vid});
          alert("已切到 " + rr.vid + "(切换前预快照 " + rr.pre_snapshot +
                ")。\n服务正在重启, 数秒后页面自动刷新。");
          setTimeout(() => location.reload(), 2500);
        } catch (e) {
          alert("切换失败: " + e.message);
          sw.disabled = false;
          sw.textContent = "切换到此版本";
        }
      };
      ops.appendChild(sw);
    }
    const df = document.createElement("button");
    df.textContent = "diff";
    df.onclick = () => showDiff(v.vid, r.versions || []);
    ops.appendChild(df);
    if (S.me && S.me.role === "admin" && !v.active) {
      const del = document.createElement("button");
      del.className = "danger";
      del.textContent = "删除";
      del.onclick = async () => {
        if (!confirm("删除 " + v.vid +
            " ?(进回收站 .trash 永不真删; 纪元首版/仅存版本/当前版本不可删)"))
          return;
        try {
          await api("/api/versions", {vid: v.vid}, "DELETE");
          loadVersions();
        } catch (e) { alert(e.message); }
      };
      ops.appendChild(del);
    }
    box.appendChild(d);
  });
  if (!box.children.length)
    box.innerHTML = '<p class="dim">还没有版本 —— 上面建第一个</p>';
  $("v-merge-btn").hidden = !(S.me && S.me.role === "admin");
  const fb = $("v-finalize-btn");
  if (fb) fb.hidden = !(S.me && S.me.role === "admin");
}
async function showDiff(vid, all) {
  const others = all.filter((v) => v.vid !== vid);
  if (!others.length) { alert("没有别的版本可比"); return; }
  const b = prompt("和哪个版本比? 输入版本号:", others[0].vid);
  if (!b) return;
  try {
    const r = await api("/api/versions/diff?a=" +
      encodeURIComponent(vid) + "&b=" + encodeURIComponent(b));
    const box = $("version-diff");
    box.hidden = false;
    const c = r.counts || {};
    const stLab = {added: "新增", removed: "移除", changed: "改动"};
    let html = "<h4>" + esc(vid) + " → " + esc(b) + " · +" + (c.added || 0) +
      " / -" + (c.removed || 0) + " / ~" + (c.changed || 0) +
      "</h4><table><thead><tr><th>文件</th><th>状态</th><th>大小</th>" +
      "</tr></thead><tbody>";
    (r.files || []).forEach((f) => {
      const sz = f.a ? f.a.bytes : f.b.bytes;
      html += "<tr><td>" + esc(f.p) + "</td><td class='st-" + f.st + "'>" +
        (stLab[f.st] || f.st) + "</td><td>" + sz + "B" +
        (f.st === "changed" ? " → " + f.b.bytes + "B" : "") + "</td></tr>";
    });
    html += "</tbody></table>";
    box.innerHTML = html;
    box.scrollIntoView({behavior: "smooth"});
  } catch (e) { alert("diff 失败: " + e.message); }
}
$("v-save").onclick = async () => {
  try {
    const r = await api("/api/versions", {
      label: $("v-label").value.trim(), note: $("v-note").value.trim()});
    $("v-msg").textContent = "已建 " + r.version.vid + " (" +
      r.version.file_count + " 文件)";
    $("v-label").value = "";
    $("v-note").value = "";
    loadVersions();
  } catch (e) { $("v-msg").textContent = "失败: " + e.message; }
};

/* ---- E4 合并版本(超管): 挑 A/B 两版 → 逐文件挑边 → 合成全新版本 ---- */
async function startMerge() {
  const all = (S.versions && S.versions.versions) || [];
  if (all.length < 2) { alert("至少要两个版本才能合并"); return; }
  const a = prompt("合并 A 版(较旧一侧)版本号:", all[0].vid);
  if (!a) return;
  const b = prompt("合并 B 版(较新一侧, 冲突默认取 B)版本号:",
    all[all.length - 1].vid);
  if (!b) return;
  if (b === a) { alert("两边不能是同一个版本"); return; }
  let r;
  try {
    r = await api("/api/versions/diff?a=" + encodeURIComponent(a) +
      "&b=" + encodeURIComponent(b));
  } catch (e) { alert("diff 失败: " + e.message); return; }
  const box = $("version-merge");
  box.hidden = false;
  const stLab = {added: "仅B有", removed: "仅A有", changed: "两边不同"};
  let html = "<h4>合并 " + esc(a) + " + " + esc(b) +
    " —— 冲突行挑一侧(默认 B), 单侧文件自动取所在侧</h4>" +
    '<table><thead><tr><th>文件</th><th>情况</th><th>取侧</th></tr>' +
    "</thead><tbody>";
  (r.files || []).forEach((f, i) => {
    html += "<tr><td>" + esc(f.p) + "</td><td class='st-" + f.st + "'>" +
      (stLab[f.st] || f.st) + "</td><td>" +
      (f.st === "changed"
        ? '<label><input type="radio" name="mk' + i + '" value="a"> A</label> ' +
          '<label><input type="radio" name="mk' + i + '" value="b" checked>' +
          " B</label>"
        : (f.st === "added" ? "B" : "A")) + "</td></tr>";
  });
  html += "</tbody></table>" +
    '<div class="row"><input id="vm-label" placeholder="合并版名称">' +
    '<button id="vm-go" class="primary">合并成新版本</button></div>' +
    '<p class="dim">只读 A/B 两版, 产出一个全新版本; 不会自动切换, 想用就切过去。</p>';
  box.innerHTML = html;
  box.scrollIntoView({behavior: "smooth"});
  $("vm-go").onclick = async () => {
    const picks = {};
    (r.files || []).forEach((f, i) => {
      const sel = document.querySelector(
        'input[name="mk' + i + '"]:checked');
      if (sel) picks[f.p] = sel.value;
    });
    try {
      const rr = await api("/api/versions/merge",
        {a, b, label: $("vm-label").value.trim(), picks});
      alert("已合并出新版本 " + rr.version.vid + " (" +
        rr.version.file_count + " 文件)。要启用就切过去。");
      box.hidden = true;
      loadVersions();
    } catch (e) { alert("合并失败: " + e.message); }
  };
}
$("v-merge-btn").onclick = () => startMerge();

/* ---- E4.1 定型(舾装, 超管): 旧版本+回收站归档, 当前工作区定为新基线 v0001 ---- */
// null 守卫: index.html 旧缓存时按钮不存在, 不能炸掉整页脚本(2026-10-20 实弹)
if ($("v-finalize-btn")) $("v-finalize-btn").onclick = async () => {
  const okc = confirm(
    "定型(舾装)?\n\n" +
    "1. 全部旧版本 + 回收站整体归档, 清单只剩新基线 v0001;\n" +
    "2. 归档后旧版本不可再切换/回退(这正是定型的含义);\n" +
    "3. 当前**运行中的工作区状态**被定为新基线 v0001;\n" +
    "4. 归档目录留在服务器 versions/archive/, 永不真删, 手工可救。");
  if (!okc) return;
  const lab = prompt("新基线名称(可空, 默认「定型」):", "定型");
  if (lab === null) return;
  try {
    const r = await api("/api/versions/finalize", {label: lab});
    alert("已定型: 新基线 " + r.version.vid +
      "\n归档 " + r.version.archived + " 项 → " + r.version.archive);
    loadVersions();
  } catch (e) { alert("定型失败: " + e.message); }
};

/* ---------------- 连接配置 ---------------- */
function renderConn(text, cls) {
  const c = $("conn"); c.textContent = text;
  c.className = "conn " + (cls || "");
}
$("btn-cfg").onclick = () => {
  $("cfg-base").value = CFG.base; $("cfg-token").value = CFG.token;
  $("cfg-mask").hidden = false; $("cfg-msg").textContent = "";
};
$("cfg-save").onclick = async () => {
  CFG.base = $("cfg-base").value.trim();
  CFG.token = $("cfg-token").value.trim();
  localStorage.setItem("at_base", CFG.base);
  localStorage.setItem("at_token", CFG.token);
  try {
    await loadModels();
    renderConn("已连接 " + (apiBase() || "(同源)"), "ok");
    $("cfg-mask").hidden = true;
    refreshSessions();
  } catch (e) {
    renderConn("连接失败", "bad");
    $("cfg-msg").textContent = "连不上: " + e.message;
  }
};

/* ---------------- E1: 登录 / 我是谁 / 超管面 ---------------- */
async function me() {
  try {
    return await api("/api/me");
  } catch (e) { return null; }
}

function renderChip(u) {
  const chip = $("user-chip");
  chip.hidden = false;
  chip.innerHTML = "<b>" + esc(u.username || "?") + "</b>" +
    '<span class="role">' + (u.role === "admin" ? "超管" : "用户") + "</span>";
  const out = document.createElement("button");
  out.textContent = "登出";
  out.onclick = () => {
    localStorage.removeItem("at_token");
    location.reload();
  };
  chip.appendChild(out);
  $("btn-admin").hidden = u.role !== "admin";
}

function showLogin(msg) {
  $("login-mask").hidden = false;
  if (msg) $("l-msg").textContent = msg;
  $("l-pass").value = "";
  $("l-user").focus();
}
$("l-go").onclick = async () => {
  try {
    const r = await fetch(apiBase() + "/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("l-user").value.trim(),
                             password: $("l-pass").value })});
    const j = await r.json();
    if (!j.ok) { $("l-msg").textContent = j.err || "登录失败"; return; }
    localStorage.setItem("at_token", j.user.token);
    location.reload();
  } catch (e) { $("l-msg").textContent = "连不上后端: " + e.message; }
};
$("l-token-mode").onclick = () => {
  const row = $("l-token-row");
  row.hidden = !row.hidden;
};
$("l-token-go").onclick = () => {
  const t = $("l-token").value.trim();
  if (!t) return;
  localStorage.setItem("at_token", t);
  location.reload();
};
$("l-pass").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("l-go").click();
});

async function loadAdmin() {
  try {
    const r = await api("/api/admin/users");
    const box = $("admin-users");
    box.innerHTML = "";
    (r.users || []).forEach((u) => {
      const d = document.createElement("div");
      d.className = "au-row";
      d.innerHTML = '<span class="nm">' + esc(u.username) + '</span>' +
        '<span class="rl">' + (u.role === "admin" ? "超管" : "用户") + "</span>" +
        (u.disabled ? '<span class="st-off">已停用</span>' : "") +
        '<span style="color:var(--dim);font-size:11px">' + esc(u.uid) +
        "</span>";
      const ops = document.createElement("span");
      ops.className = "ops";
      const mk = (lab, fn) => {
        const b = document.createElement("button");
        b.textContent = lab;
        b.onclick = () => fn(u);
        ops.appendChild(b);
      };
      mk(u.disabled ? "启用" : "停用", (u) =>
        api("/api/admin/user", { username: u.username,
          op: u.disabled ? "enable" : "disable" }).then(loadAdmin));
      mk("改密", (u) => {
        const p = prompt("给 " + u.username + " 设新密码:");
        if (p) api("/api/admin/user", { username: u.username, op: "password",
          password: p }).then(loadAdmin);
      });
      mk("重置令牌", (u) =>
        api("/api/admin/user", { username: u.username, op: "token" })
          .then((r) => alert(u.username + " 的新令牌:\n" + r.token))
          .then(loadAdmin));
      d.appendChild(ops);
      box.appendChild(d);
    });
    if (!box.children.length) box.innerHTML =
      '<p class="dim">还没有用户</p>';
    const a = await api("/api/admin/audit?limit=60");
    const ab = $("admin-audit");
    ab.innerHTML = "";
    (a.audit || []).forEach((row) => {
      const d = document.createElement("div");
      d.className = "audit-row";
      const t = row.ts ? new Date(row.ts * 1000).toLocaleString("zh-CN",
        { hour12: false }) : "";
      d.innerHTML = '<span class="t">' + esc(t) + '</span>' +
        '<span class="a">' + esc(row.actor) + '</span>' + esc(row.action) +
        " " + esc(row.detail || "");
      ab.appendChild(d);
    });
  } catch (e) { $("admin-users").innerHTML =
    '<p class="dim">读取失败: ' + esc(e.message) + "</p>"; }
}
$("a-save").onclick = async () => {
  try {
    const r = await api("/api/admin/users", {
      username: $("a-name").value.trim(), password: $("a-pass").value,
      role: $("a-role").value });
    $("a-msg").textContent = r.ok ? ("已开户 " + r.user.username +
      ", 令牌: " + r.user.token) : (r.err || "失败");
    $("a-pass").value = "";
    loadAdmin();
  } catch (e) { $("a-msg").textContent = e.message; }
};

/* ---------------- 启动 ---------------- */
(async function boot() {
  if (!CFG.token) {                       // 没令牌: 先登录
    showLogin();
    try { await loadModels(); } catch (e) {}
    return;
  }
  try {
    await loadModels();
  } catch (e) {
    if (String(e.message).indexOf("令牌") >= 0 ||
        String(e.message).indexOf("HTTP 401") >= 0) {
      showLogin("令牌失效, 重新登录");
    } else {
      renderConn("未连接: " + e.message, "bad");
      $("cfg-mask").hidden = false;
    }
    return;
  }
  const u = await me();
  if (!u) { showLogin("令牌失效, 重新登录"); return; }
  S.me = u;
  renderChip(u);
  renderConn("已连接 " + (apiBase() || "(同源)"), "ok");
  refreshSessions();
  setInterval(refreshSessions, 8000);
})();

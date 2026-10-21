# -*- coding: utf-8 -*-
"""version.py — E2 版本系统: 全局快照多版本(桌面 M12a 思想的服务化).

口径(ROADMAP-E 拍板):
- 快照对象 = backend 全目录(core + server 等); data/ 在树外(E1.7), 物理绝缘.
- 建版本: 全树逐文件 sha1 存 manifest.json + 拷贝到 versions/<vid>/files/.
- 切版本: **滚前自动快照当前树**(家法, 永远有回头路) → 快照文件覆盖工作区
  (**只覆盖, 永不删除** —— 工作区里快照没有的文件原样保留) → 服务重启
  (AT_RESTART_CMD, 秒级闪断, 如实告知用户). 全局生效, 影响所有人.
- 删除: 仅超管, 目录挪进 versions/.trash 永不真删; 纪元首版/仅存版本/当前
  激活版本不可删; 删过的号不回收.
- 登记进 webusers 库(sqlite/PG 同源), 每个动作一行审计.

env 口子(自检沙箱用): AT_VERSIONS_DIR / AT_BACKEND_ROOT / AT_RESTART_CMD.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

import webusers

HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.environ.get("AT_VERSIONS_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "versions"))
BACKEND_ROOT = os.environ.get("AT_BACKEND_ROOT") or HERE
RESTART_CMD = os.environ.get("AT_RESTART_CMD", "").strip()
ACTIVE_FILE = os.path.join(VERSIONS_DIR, "ACTIVE")
RESTART_MARKER = os.path.join(VERSIONS_DIR, "RESTART.marker")
RESTART_GO = os.path.join(VERSIONS_DIR, "RESTART.go")   # 2026-10-20: path unit 触发

_SKIP_DIRS = {"__pycache__", "data", "run", "versions", ".trash", ".git"}


# ---------------- 工作区扫描 ----------------
def _iter_files():
    """backend 根下参与快照的文件(相对路径, 正斜杠), 跳过运行时目录."""
    out = []
    for base, dirs, names in os.walk(BACKEND_ROOT):
        rel_base = os.path.relpath(base, BACKEND_ROOT)
        top = rel_base.split(os.sep)[0] if rel_base != "." else ""
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in sorted(names):
            if n.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(os.path.join(base, n), BACKEND_ROOT)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _sha1_bytes(b):
    return hashlib.sha1(b).hexdigest()


def _read_manifest(vid):
    fp = os.path.join(VERSIONS_DIR, vid, "manifest.json")
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def active_version():
    try:
        with open(ACTIVE_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        return v if v else ""
    except Exception:
        return ""


def _set_active(vid):
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(vid)


def _next_vid():
    """下一个可用版本号(删过的号不回收 —— 含 .trash 的都在库里)."""
    with webusers._lock, webusers._db() as c:
        rows = webusers._sql(c, "SELECT vid FROM versions")
    used = {r["vid"] for r in rows or []}
    n = 1
    while "v%04d" % n in used:
        n += 1
    return "v%04d" % n


def _write_manifest_and_register(man, entries, total):
    """manifest.json 落盘 + 库登记(建版本/合并共用)."""
    with open(os.path.join(VERSIONS_DIR, man["vid"], "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    with webusers._lock, webusers._db() as c:
        webusers._sql(c, "INSERT INTO versions(vid,label,note,created_by,"
                         "created_at,file_count,total_bytes,trashed)"
                         " VALUES(?,?,?,?,?,?,?,0)",
                      (man["vid"], man["label"], man["note"],
                       man["created_by"], man["created_at"],
                       man["file_count"], man["total_bytes"]))


# ---------------- 建版本 ----------------
def create(label, note, actor):
    """当前工作区 → 新快照. 返回 (ok, 信息|错误)."""
    files = _iter_files()
    if not files:
        return False, "工作区是空的(根目录指错?)"
    vid = _next_vid()

    vdir = os.path.join(VERSIONS_DIR, vid, "files")
    os.makedirs(vdir, exist_ok=True)
    entries, total = [], 0
    for rel in files:
        src = os.path.join(BACKEND_ROOT, rel.replace("/", os.sep))
        with open(src, "rb") as f:
            raw = f.read()
        dst = os.path.join(vdir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(raw)
        entries.append({"p": rel, "sha1": _sha1_bytes(raw),
                        "bytes": len(raw)})
        total += len(raw)
    man = {"vid": vid, "label": str(label or "")[:80],
           "note": str(note or "")[:500], "created_by": str(actor or "")[:64],
           "created_at": time.time(), "active_at_create": active_version(),
           "file_count": len(entries), "total_bytes": total,
           "files": entries}
    _write_manifest_and_register(man, entries, total)
    webusers.audit(actor, "version_create",
                   "%s(%d文件)" % (vid, len(entries)))
    return True, {"vid": vid, "label": man["label"],
                  "file_count": len(entries), "total_bytes": total}


# ---------------- 清单 / diff ----------------
def listing():
    with webusers._lock, webusers._db() as c:
        rows = webusers._sql(c, "SELECT vid,label,note,created_by,created_at,"
                                "file_count,total_bytes FROM versions"
                                " WHERE trashed=0 ORDER BY vid")
    act = active_version()
    out = [{"vid": r["vid"], "label": r["label"], "note": r["note"],
            "created_by": r["created_by"], "created_at": r["created_at"],
            "file_count": r["file_count"], "total_bytes": r["total_bytes"],
            "e6": _snapshot_is_e6(r["vid"]),
            "active": r["vid"] == act} for r in rows or []]
    # 2026-10-20 用户拍板: 切换前自动快照置顶(只留最新一份, 见 switch_to)
    snaps = [x for x in out if x["label"] == "切换前自动快照"]
    rest = [x for x in out if x["label"] != "切换前自动快照"]
    return snaps + rest


def _snapshot_is_e6(vid):
    """该版本快照是否含项目分域(E6 的 /api/projects 路由). 旧版切过去后项目数据
    暂时不可见 —— 前端据此给「旧版」标记 + 切换警告(2026-10-20 用户被吓过一次)."""
    try:
        fp = os.path.join(VERSIONS_DIR, vid, "files", "server.py")
        with open(fp, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
        return "api/projects" in head
    except OSError:
        return True     # 读不到就按新版对待, 不误伤


def diff(va, vb):
    """事无巨细: 两版本 manifest 逐文件对比."""
    ma, mb = _read_manifest(va), _read_manifest(vb)
    if ma is None or mb is None:
        return False, "版本不存在或 manifest 坏了"
    fa = {e["p"]: e for e in ma.get("files", [])}
    fb = {e["p"]: e for e in mb.get("files", [])}
    out = []
    for p in sorted(set(fa) | set(fb)):
        a, b = fa.get(p), fb.get(p)
        if a and not b:
            out.append({"p": p, "st": "removed", "a": a, "b": None})
        elif b and not a:
            out.append({"p": p, "st": "added", "a": None, "b": b})
        elif a["sha1"] != b["sha1"]:
            out.append({"p": p, "st": "changed", "a": a, "b": b})
    counts = {k: sum(1 for x in out if x["st"] == k)
              for k in ("added", "removed", "changed")}
    return True, {"a": va, "b": vb, "files": out, "counts": counts}


# ---------------- 切换(全局生效) ----------------
def _can_write_root():
    """预检: 工作区可写吗(切换要覆盖文件; root 属主的树在这里原形毕露)."""
    try:
        probe = os.path.join(BACKEND_ROOT, ".atweb_write_probe")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True
    except Exception:
        return False


def switch_to(vid, actor):
    man = _read_manifest(vid)
    if man is None:
        return False, "没有这个版本"
    if vid == active_version():
        return True, {"vid": vid, "pre_snapshot": "", "note": "已是当前版本"}
    if not _can_write_root():           # 2026-10-19 线上实弹: root 属主的树
        return False, ("工作区目录不可写(部署时 backend 树归属错了, 应"
                       " chown 服务用户) —— 已在快照前拦截, 未产生自动快照")
    pre = ""
    ok, r = create("切换前自动快照", "switch → %s" % vid, actor)
    if ok:                              # 家法: 滚前自动快照(失败则中止)
        pre = r["vid"]
    else:
        return False, "滚前自动快照失败, 已中止: %s" % r
    vdir = os.path.join(VERSIONS_DIR, vid, "files")
    try:
        for e in man.get("files", []):  # 只覆盖, 永不删除
            src = os.path.join(vdir, e["p"].replace("/", os.sep))
            if not os.path.isfile(src):
                continue                # manifest 有但文件缺: 跳过并如实留痕
            dst = os.path.join(BACKEND_ROOT, e["p"].replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as f:
                raw = f.read()
            with open(dst, "wb") as f:
                f.write(raw)
    except Exception as ex:             # 写到一半也要如实报告+指条回头路
        return False, ("切换中断, 工作区可能不完整: %r —— 预快照 %s "
                       "在版本清单里, 可再试切换或手工恢复" % (ex, pre))
    _set_active(vid)
    with open(RESTART_MARKER, "w", encoding="utf-8") as f:
        f.write("%s %s %s" % (time.time(), actor, vid))
    # 2026-10-20 重启机制去 sudoers 化: 有 AT_RESTART_CMD 走老口径(自检/自装),
    # 否则只写 RESTART.go —— systemd path unit(以 root)监到即重启服务,
    # 不再依赖 sudoers(曾两次凭空消失, 切换重启静默失效全天).
    if RESTART_CMD:                     # 1.5s 缓冲让 HTTP 响应先走完
        subprocess.Popen(
            "sleep 1.5; " + RESTART_CMD
            + " >> %s 2>&1" % os.path.join(VERSIONS_DIR, "restart.log"),
            shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(               # 真写入(不是 touch: 纯 utimensat 不产生
            "sleep 1.5; date +%s > " + RESTART_GO,   # inotify 写事件, path unit 不响)
            shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 2026-10-20 用户拍板: 切换前自动快照只留最新一份(旧的进 .trash 永不真删)
    with webusers._lock, webusers._db() as c:
        rows = webusers._sql(c, "SELECT vid,label FROM versions"
                                " WHERE trashed=0")
    for r in rows or []:
        if r.get("label") == "切换前自动快照" and r.get("vid") != pre:
            try:
                delete(r["vid"])
            except Exception:
                pass
    webusers.audit(actor, "version_switch", "%s ← %s(预快照%s)"
                   % (vid, man.get("active_at_create") or "?", pre))
    return True, {"vid": vid, "pre_snapshot": pre,
                  "restart": bool(RESTART_CMD),
                  "note": "已覆盖工作区并请求重启, 服务将闪断数秒后以新版本回来"}


# ---------------- 删除(仅超管; 进 .trash 永不真删) ----------------
def delete(vid):
    with webusers._lock, webusers._db() as c:
        rows = webusers._sql(c, "SELECT vid,trashed FROM versions"
                                " ORDER BY vid")
    live = [r["vid"] for r in rows or [] if not r["trashed"]]
    if vid not in live:
        return False, "没有这个版本(或已在回收站)"
    if _read_manifest(vid) is None:
        return False, "manifest 缺失, 拒绝动"
    if vid == live[0]:
        return False, "纪元首版不可删(家法)"
    if len(live) <= 1:
        return False, "仅存版本不可删"
    if vid == active_version():
        return False, "当前激活版本不可删(先切走再删)"
    trash = os.path.join(VERSIONS_DIR, ".trash")
    os.makedirs(trash, exist_ok=True)
    src = os.path.join(VERSIONS_DIR, vid)
    dst = os.path.join(trash, vid)
    if os.path.isdir(dst):
        shutil.rmtree(dst)             # 回收站里的同名旧残留(号不回收, 理论少见)
    shutil.move(src, dst)
    with webusers._lock, webusers._db() as c:
        webusers._exec(c, "UPDATE versions SET trashed=1 WHERE vid=?", (vid,))
    webusers.audit("admin", "version_delete", vid)
    return True, {"vid": vid, "note": "已进 .trash, 永不真删"}


# ---------------- 合并(仅超管入口; 逐文件挑 A/B 合成全新版本) ----------------
def merge(va, vb, label, note, actor, picks=None):
    """E4 务实口径: 两版本逐文件合并 —— picks={path:'a'|'b'} 显式挑边;
    缺省: 单侧文件取所在侧, 双侧都在取 B(视为较新).
    安全线: 只读 A/B 快照, 产出一个全新版本, **绝不碰 A/B 和工作区**;
    每个文件拷完 sha1 必须与源 manifest 一致, 中途失败清场不留半成品."""
    ma, mb = _read_manifest(va), _read_manifest(vb)
    if ma is None or mb is None:
        return False, "版本不存在或 manifest 坏了"
    if va == vb:
        return False, "同一个版本没得合并"
    picks = picks if isinstance(picks, dict) else {}
    fa = {e["p"]: e for e in ma.get("files", [])}
    fb = {e["p"]: e for e in mb.get("files", [])}
    chosen = []
    for p in sorted(set(fa) | set(fb)):
        a, b = fa.get(p), fb.get(p)
        side = str(picks.get(p) or "").strip().lower()
        if side == "a" and a is None:
            side = ""
        if side == "b" and b is None:
            side = ""
        if side not in ("a", "b"):
            side = "b" if b is not None else "a"   # 缺省: 有 B 取 B
        chosen.append((p, side, a if side == "a" else b))
    if not chosen:
        return False, "两版合起来是空的"

    vid = _next_vid()
    vdir = os.path.join(VERSIONS_DIR, vid, "files")
    os.makedirs(vdir, exist_ok=True)
    roots = {"a": os.path.join(VERSIONS_DIR, va, "files"),
             "b": os.path.join(VERSIONS_DIR, vb, "files")}
    entries, total = [], 0
    try:
        for p, side, e in chosen:
            src = os.path.join(roots[side], p.replace("/", os.sep))
            if not os.path.isfile(src):
                raise IOError("源文件缺失: %s(%s 侧)" % (p, side))
            with open(src, "rb") as f:
                raw = f.read()
            got = _sha1_bytes(raw)
            if got != e.get("sha1"):
                raise IOError("sha1 对不上: %s" % p)
            dst = os.path.join(vdir, p.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(raw)
            entries.append({"p": p, "sha1": got, "bytes": len(raw)})
            total += len(raw)
    except Exception as ex:                    # 清场: 不留半成品版本
        shutil.rmtree(os.path.join(VERSIONS_DIR, vid), ignore_errors=True)
        return False, "合并中断(已清场, 无半成品): %s" % ex
    man = {"vid": vid, "label": str(label or "")[:80],
           "note": ("%s | merge %s+%s" % (str(note or ""), va, vb))[:500],
           "created_by": str(actor or "")[:64], "created_at": time.time(),
           "active_at_create": active_version(),
           "file_count": len(entries), "total_bytes": total,
           "files": entries}
    _write_manifest_and_register(man, entries, total)
    webusers.audit(actor, "version_merge",
                   "%s=%s+%s(%d文件)" % (vid, va, vb, len(entries)))
    return True, {"vid": vid, "label": man["label"],
                  "file_count": len(entries), "total_bytes": total}


# ---------------- 定型(仅超管入口; 桌面 finalize 同口径, 纪元重开) ----------------
def finalize(label, note, actor):
    """定型: 全部旧版本(含 .trash)整体归档进 versions/archive/<时间戳>/
    (永不真删, 后悔药), 当前**运行中的工作区状态**建成新基线 v0001(舾装).
    编号跨纪元重启(archive 不参与高水位), 纪元内"删过的号不回收"照旧;
    定型后旧版本不再出现在清单、不可再切换回退 —— 这正是定型的含义.
    安全线: 归档只移不删; 挪失败不动数据库(已挪的在 archive, 无损可手工
    归位); 清库后复用 create() 见空表自然出 v0001, 零硬编码.
    返回 (ok, {"vid","archive","archived"}|错误)."""
    with webusers._lock, webusers._db() as c:
        rows = webusers._sql(c, "SELECT vid,label,note,created_by,"
                                "created_at,trashed FROM versions")
    live = [r["vid"] for r in rows or [] if not r["trashed"]]
    if not live:
        return False, "没有任何版本, 无从定型"
    if len(live) == 1 and not any(r["trashed"] for r in rows or []):
        return False, ("只有一个基线版本, 没有可合并定型的改动"
                       "(先建/合并出新版本再来定型)")
    ts = time.strftime("%Y%m%d-%H%M%S")
    arch = os.path.join(VERSIONS_DIR, "archive", ts)
    moved = []
    try:
        os.makedirs(arch, exist_ok=True)
        for vid in live:                       # 每个活版本目录整体挪进归档
            src = os.path.join(VERSIONS_DIR, vid)
            if os.path.isdir(src):
                shutil.move(src, os.path.join(arch, vid))
                moved.append(vid)
        trash = os.path.join(VERSIONS_DIR, ".trash")
        if os.path.isdir(trash):               # 回收站随纪元归档 —— 否则
            shutil.move(trash, os.path.join(arch, "trash"))
            moved.append(".trash")
    except Exception as ex:
        return False, ("归档中断(已挪的都在 %s, 无损可手工归位): %s"
                       % (arch, ex))
    try:                                       # 后悔药元数据(web 没有
        with open(os.path.join(arch, "index.json"), "w",    # manifest.md,
                  encoding="utf-8") as f:      # index.json 记下归档了什么)
            json.dump({"archived_at": ts,
                       "active_at_finalize": active_version(),
                       "moved": moved,
                       "versions": rows or []},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    with webusers._lock, webusers._db() as c:  # 清库(audit 表不动, 全程
        webusers._exec(c, "DELETE FROM versions")     # 动作都有审计)
    ok, r = create(str(label or "定型")[:80],
                   "%s | 定型归档 archive/%s" % (str(note or ""), ts), actor)
    if not ok:
        return False, ("定型中断: %s —— 旧版本已在 %s, 手工可救"
                       % (r, arch))
    _set_active(r["vid"])
    webusers.audit(actor, "version_finalize",
                   "%s 新基线(舾装) · 归档 %d 项 → %s"
                   % (r["vid"], len(moved), arch))
    return True, {"vid": r["vid"], "archive": arch, "archived": len(moved)}

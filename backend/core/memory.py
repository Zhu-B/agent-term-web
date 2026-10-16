# -*- coding: utf-8 -*-
"""memory.py — 跨会话记忆卡(M9): 共享记忆库的存储层.

定位: 所有聊天窗口/所有 agent 共用的一叠"记忆卡" —— 每张卡 = 一个 .md 文件,
文件名(去后缀)= 卡名. 内容是给 agent 看的持久事实/偏好/项目背景, 注入会话
即被该会话的 agent 读到(不可撤销: 上下文只能越压越实, 没有"吐回去").

与 AGENT.md 的分工: AGENT.md = 每任务自动注入的静态约定; 记忆卡 = 按需手动/
工具注入的动态事实. 谁在什么时候读了哪张卡, 走 memory_read 事件落 trace
(研究口径: 记忆消费行为可回放).

家法: 目录在 ~/.agent-term/memory/(仓库外); AT_MEMORY_DIR 可重定向(测试缝).

分区(M9.1 忆库): 留档区 archive = MEM_DIR 根; 遗忘区 forget = MEM_DIR/forget/.
两区**都随时可读**(聊天窗卡列 / agent 的 memory 工具) —— 区别只在遗忘区的卡
每逢周末自动清理(用户 10-10 拍板: 遗忘≠不可读, 只是会过期).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

MEM_DIR = os.environ.get("AT_MEMORY_DIR") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "memory")

# 写锁(2026-10-13 多 agent 并发防护): 多个聊天窗/轮末自动记忆可能同时写同一张卡 ——
# 同进程内所有写路径串行化(轮转+写入原子), 杜绝交错写坏. 跨进程(CLI+GUI 同时开)
# 由 write_card_guarded 的版本检查兜底: 撞车不盲盖, 改另存新卡+警示.
_WRITE_LOCK = threading.RLock()

TITLE_MAX = 40          # 卡名长度上限(列宽有限, 超长截断显示, 存储仍全名)
CONTENT_CAP = 8000      # 单卡读取注入上限(防爆上下文; 文件本体不截)
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _zone_dir(zone: str = "archive") -> str:
    return MEM_DIR if zone == "archive" else os.path.join(MEM_DIR, "forget")


def _safe_title(title: str) -> str:
    """卡名 → 安全文件名词: 去非法字符/首尾空白, 空结果返回 ''."""
    return _BAD.sub("", (title or "").strip())


def list_cards(zone: str = "all") -> list:
    """卡片清单: [{title, mtime, size, head, zone}], 按修改时间倒序(新卡在前).
    zone = "all"(默认: 两区合并, 每张带 zone 标) | "archive" | "forget"."""
    out = []
    for z in (("archive", "forget") if zone == "all" else (zone,)):
        d = _zone_dir(z)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md"):
                continue
            path = os.path.join(d, name)
            try:
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = (f.read(120) or "").strip().replace("\n", " ")
            except OSError:
                continue
            out.append({"title": name[:-3], "mtime": stat.st_mtime,
                        "size": stat.st_size, "head": head, "zone": z})
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out


def find_zone(title: str) -> str:
    """这张卡在哪区: "archive" | "forget" | ""(没有)."""
    t = _safe_title(title)
    for z in ("archive", "forget"):
        if t and os.path.isfile(os.path.join(_zone_dir(z), t + ".md")):
            return z
    return ""


def read_card(title: str, zone: str = "") -> str:
    """读一张卡(整卡文本, 超 CONTENT_CAP 截断加标记). zone 空=两区都找(留档优先);
    没有这张卡返回 ''."""
    t = _safe_title(title)
    if not t:
        return ""
    for z in ("archive", "forget") if not zone else (zone,):
        try:
            with open(os.path.join(_zone_dir(z), t + ".md"), encoding="utf-8",
                      errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if len(text) > CONTENT_CAP:
            text = text[:CONTENT_CAP] + "\n…(该卡超 %d 字符已截断)" % CONTENT_CAP
        return text
    return ""


def card_exists(title: str, zone: str = "") -> bool:
    """卡是否存在. zone 空=两区任一存在(默认); 指定 zone=只看那区."""
    t = _safe_title(title)
    if not t:
        return False
    zs = ("archive", "forget") if not zone else (zone,)
    return any(os.path.isfile(os.path.join(_zone_dir(z), t + ".md")) for z in zs)


def norm_title(title: str) -> str:
    """卡名规范形(与落盘文件名同规) —— 工具层/版本检查/裁决共用同一把尺."""
    return _safe_title(title)


def card_version(title: str, zone: str = "") -> str:
    """该卡主文件内容的 sha1(不存在返 ""). 乐观并发检查的"版本号" —— 用内容而非
    mtime: Windows 时间戳粒度/惰性刷新会骗人(2026-10-13 实测撞车漏检, 秒内两次
    写 getmtime 相同)."""
    t = _safe_title(title)
    if not t:
        return ""
    for z in (("archive", "forget") if not zone else (zone,)):
        p = os.path.join(_zone_dir(z), t + ".md")
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    return hashlib.sha1(f.read()).hexdigest()
            except OSError:
                return ""
    return ""


def _write_locked(t: str, content: str, zone: str) -> str:
    """锁内写卡(假定 t 已过名检): 覆写先轮转 .bak, 再落新内容."""
    d = _zone_dir(zone)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, t + ".md")
    if os.path.exists(path):                 # 覆写留底: 记忆被改也可追溯
        bak = path + ".bak"
        n = 1
        while os.path.exists(bak):
            n += 1
            bak = "%s.bak-%d" % (path, n)
        try:
            os.replace(path, bak)
        except OSError:
            # 改名在 Windows 共享模式下会因"文件被其它句柄打开"失败(2026-10-13 实锤:
            # 曾静默吞异常→原件直接丢). 兜底改成拷贝留底(读共享下可成), 绝不裸覆写
            try:
                with open(path, "rb") as src, open(bak, "wb") as dst:
                    dst.write(src.read())
            except OSError:
                pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return t


def write_card(title: str, content: str, zone: str = "archive") -> str:
    """新建/覆写一张卡(覆写前留 .bak, 与 write 工具同规矩). 返回安全卡名,
    名字非法/为空返回 ''."""
    with _WRITE_LOCK:
        t = _safe_title(title)
        if not t:
            return ""
        return _write_locked(t, content, zone)


def write_card_guarded(title: str, content: str, zone: str = "archive",
                       expect_version: str = None) -> tuple:
    """带版本检查的写卡(多 agent 撞车防护, 检查+写入在锁内=原子):
    expect_version=None 普通写; ="" 期望"还不存在"; =sha1 期望"仍是我读过那版".
    返回 (卡名, 说明): 成功 ("t",""); 撞车 ("","raced"); 名非法 ("","badname")."""
    with _WRITE_LOCK:
        t = _safe_title(title)
        if not t:
            return "", "badname"
        if expect_version is not None:
            if card_version(t, zone=zone) != expect_version:
                return "", "raced"
        return _write_locked(t, content, zone), ""


def move_zone(title: str, src: str, dst: str) -> str:
    """卡换区(留档↔遗忘). 目标区同名卡不覆盖(加序号). 返回落位卡名, 失败 ''."""
    t = _safe_title(title)
    if not t or src == dst:
        return ""
    with _WRITE_LOCK:
        sp = os.path.join(_zone_dir(src), t + ".md")
        if not os.path.isfile(sp):
            return ""
        os.makedirs(_zone_dir(dst), exist_ok=True)
        final, dp = t, os.path.join(_zone_dir(dst), t + ".md")
        n = 1
        while os.path.exists(dp):
            n += 1
            final = "%s-%d" % (t, n)
            dp = os.path.join(_zone_dir(dst), final + ".md")
        try:
            os.replace(sp, dp)
        except OSError:
            return ""
    return final


def similar_title(a: str, b: str) -> bool:
    """卡名近似判定(机械, 纯字符串): 清洗后互为子串且较短者 ≥2 字 —— 轮末自动记忆
    "疑似重复"提示用. 语义级判断(该不该合并)是 agent 的活, 这里只做保守提醒."""
    x, y = _safe_title(a or ""), _safe_title(b or "")
    if not x or not y or x == y:
        return False
    if len(x) > len(y):
        x, y = y, x
    return len(x) >= 2 and x in y


def delete_card(title: str, zone: str = "archive", keep_bak: bool = False) -> bool:
    """删除卡. keep_bak=False(默认, 忆库页✕): 彻底删除, 连覆写历史(.bak*)一并扫掉.
    keep_bak=True(合并 merge_from): 只删主文件, **保证一份 .bak 留底**可恢复 ——
    源卡往往从未被覆写过(本来没 .bak), 先补一份再删, "删得掉也回得来"."""
    t = _safe_title(title)
    if not t:
        return False
    with _WRITE_LOCK:
        d = _zone_dir(zone)
        p = os.path.join(d, t + ".md")
        if not os.path.exists(p):
            return False
        import glob as _g
        if keep_bak and not _g.glob(p + ".bak*"):
            try:
                with open(p, "rb") as src, open(p + ".bak", "wb") as dst:
                    dst.write(src.read())
            except OSError:
                pass
        try:
            os.remove(p)
        except OSError:
            return False
        if not keep_bak:
            for tail in _g.glob(os.path.join(d, t + ".md.bak*")):
                try:
                    os.remove(tail)
                except OSError:
                    pass
        return True


def revert_overwrite(title: str) -> tuple:
    """撤回一次覆盖(2026-10-13 用户拍板): 主文件里的"新内容"另存为「title（更新）」,
    最新 .bak(被覆盖掉的旧版)写回主文件. 返回 (ok, 人话说明).
    没有 .bak / 卡不在 → (False, 原因); 任何一步失败不动数据."""
    t = _safe_title(title)
    if not t:
        return False, "卡名无效"
    with _WRITE_LOCK:               # 读旧+两连写必须原子: 防另一窗口在两写之间插队
        zone = find_zone(t)
        if not zone:
            return False, "找不到这张卡"
        p = os.path.join(_zone_dir(zone), t + ".md")
        import glob as _g
        baks = _g.glob(p + ".bak*")
        if not baks:
            return False, "这张卡没有留底(.bak), 撤不了"

        def _suffix(f):             # .bak→1, .bak-N→N: 最大后缀 = 最近一次覆盖前的版本
            m = re.search(r"\.bak(?:-(\d+))?$", f)
            return int(m.group(1) or 1) if m else 0

        newest = max(baks, key=_suffix)
        try:
            with open(p, encoding="utf-8") as f:
                new_content = f.read()
            with open(newest, encoding="utf-8") as f:
                old_content = f.read()
        except OSError as ex:
            return False, "读卡失败: %s" % ex
        cand, n = "%s（更新）" % t, 2
        while card_exists(cand):
            cand = "%s（更新%d）" % (t, n)
            n += 1
        if not write_card(cand, new_content):   # 先保新内容(另存新卡, 不动原卡)
            return False, "新内容另存失败, 未做任何改动"
        if not write_card(t, old_content):      # 旧版写回主文件(现行内容自动轮转入 .bak)
            delete_card(cand, zone=find_zone(cand) or "archive", keep_bak=True)
            return False, "写回旧版失败, 已撤销另存"
        return True, "已撤回: 旧版内容已写回「%s」; 被撤下的新内容另存为「%s」" % (t, cand)


def merge_cards(target: str, sources: list) -> tuple:
    """忆库页「合并」(2026-10-14 用户拍板): 把源卡内容并入目标卡(每张源卡带
    「### 卡名」分隔头), 源卡移入遗忘区 —— 页面上看得见、可拖回留档区恢复、
    周末自动清理(与 agent 的 merge_from 不同: 那是删源留 .bak).
    目标卡覆写留 .bak(同写卡规矩). 返回 (ok, 人话说明)."""
    t = _safe_title(target)
    if not t:
        return False, "目标卡名无效"
    with _WRITE_LOCK:
        body = read_card(t)
        if not body:
            return False, "目标卡「%s」不存在" % t
        parts = [body]
        merged, missing = [], []
        for src_raw in (sources or [])[:10]:       # 上限 10 张, 防手滑全选
            s = _safe_title(src_raw)
            if not s or s == t:
                continue
            src_body = read_card(s)
            if not src_body:
                missing.append(s)
                continue
            parts.append("\n\n### %s\n%s" % (s, src_body))
            merged.append(s)
        if not merged:
            return False, "没有可合并的源卡(都找不到, 或就是目标卡本身)"
        if not write_card(t, "\n".join(parts)):     # 先落目标(失败则一张不动)
            return False, "写回目标卡失败, 未做任何改动"
        for s in merged:                            # 后移源卡进遗忘区
            z = find_zone(s)
            if z and z != "forget":
                move_zone(s, z, "forget")
        note = ("已把 %d 张并入「%s」(目标旧版已留 .bak; 源卡已移入遗忘区, "
                "周末自动清理, 拖回留档区可恢复): %s"
                % (len(merged), t, "、".join(merged)))
        if missing:
            note += "; 没找到未动: %s" % "、".join(missing)
        return True, note


def purge_forget() -> int:
    """清空遗忘区(硬删全部 .md). 返回清掉的卡数."""
    with _WRITE_LOCK:
        d = _zone_dir("forget")
        try:
            names = os.listdir(d)
        except OSError:
            return 0
        n = 0
        for name in names:
            if name.endswith(".md"):
                try:
                    os.remove(os.path.join(d, name))
                    n += 1
                except OSError:
                    pass
        return n


def weekend_purge(today=None) -> str:
    """周末自动清理遗忘区: 周六/周日调用才动手, 每个 ISO 周最多一次
    (标记 .last_purge). 非周末调用=不动. 返回结果一句话(today 参数供测试)."""
    import datetime
    d = today or datetime.date.today()
    if d.weekday() < 5:
        return "非周末, 遗忘区不动"
    os.makedirs(MEM_DIR, exist_ok=True)
    marker = os.path.join(MEM_DIR, ".last_purge")
    iso = d.isocalendar()
    week = "%04d-W%02d" % (iso[0], iso[1])
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == week:
                return "本周已清过, 遗忘区不动"
    except OSError:
        pass
    n = purge_forget()
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(week)
    except OSError:
        pass
    return "周末清理: 遗忘区清掉 %d 张" % n


def fmt_mtime(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))

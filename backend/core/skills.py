# -*- coding: utf-8 -*-
"""skills.py — 技能包(M17): 打包好的操作手册的存储层.

定位: 技能 = 一份 .md 文件, frontmatter 给 name(文件名词干, kebab-case) +
description(一句话用途), 正文 = 给 agent 的工作规程. agent 在任务对得上时用
skill 工具 action=load 把正文读进上下文照着做 —— 纯指令包: 干活仍走 bash/edit
等现有工具与权限门, 技能文件本身永远不会被直接执行.

两层(2026-10-14 用户拍板): 仓库 skills/(随软件发布、预置、git 跟踪的精选集) +
~/.agent-term/skills/(个人技能). 合并列出, **同名个人技能覆盖仓库技能**(仓库那条
标 shadowed, 系统提示词清单只列生效版). 个人层在 .agent-term 树下, agent 的
read/write/edit/bash 工具被存储守卫硬拒碰不到 —— 只有 skill 工具和技能页能读写它.

家法: AT_SKILLS_DIR / AT_REPO_SKILLS_DIR 可重定向(测试缝); 坏文件/坏目录一律
跳过不抛(系统提示词路径绝不炸); 覆写轮转 .bak(与 memory 同规矩), 删除进
<层>/.trash/(不真删). skills/ 目录不进版本快照(watched_files 只管仓库根层文件,
kb 同待遇) —— 回滚不删也不还原本体, 与 kb 同口径.
"""
from __future__ import annotations

import os
import re
import threading
import time

REPO_SKILLS_DIR = os.environ.get("AT_REPO_SKILLS_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "skills")
USER_SKILLS_DIR = os.environ.get("AT_SKILLS_DIR") or os.path.join(
    os.path.expanduser("~"), ".agent-term", "skills")

# 写锁: 技能页与工具写路径串行化(与 memory 同规矩, 防交错写坏)
_WRITE_LOCK = threading.RLock()

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CONTENT_CAP = 8000      # 单技能读入上限(防爆上下文; 文件本体不截)
DESC_CAP = 100          # 提示词清单里的描述截断


def parse_frontmatter(text: str) -> tuple:
    """解析技能文件 → (meta, body, err). 首行必须 '---', 下一个 '---' 行收尾;
    meta 必须含 name(kebab-case) 与 description(非空单行). 坏文件 → (None, "", 原因)."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "", "首行必须是 '---'"
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, "", "frontmatter 缺收尾 '---'"
    meta = {}
    for ln in lines[1:end]:
        if ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        meta[k.strip().lower()] = v.strip()
    name = meta.get("name", "")
    if not NAME_RE.match(name):
        return None, "", "name 只许小写字母/数字/连字符(如 three-piece-suite)"
    if not meta.get("description"):
        return None, "", "缺 description(一句话用途, 用于清单展示)"
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body, ""


def _layer_dir(source: str) -> str:
    return USER_SKILLS_DIR if source == "user" else REPO_SKILLS_DIR


def _scan(source: str) -> list:
    """某层技能目录 → [(name, meta, path, mtime, size)]; 坏文件/坏目录跳过不抛
    (list_skills 供系统提示词路径用, 绝不炸)."""
    d = _layer_dir(source)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for fname in names:
        if not fname.endswith(".md"):
            continue
        path = os.path.join(d, fname)
        try:
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                meta, _, err = parse_frontmatter(f.read())
        except OSError:
            continue
        if err or (meta.get("name") or "") != fname[:-3]:
            continue        # 名不对版/坏文件: 清单跳过
        out.append((fname[:-3], meta, path, stat.st_mtime, stat.st_size))
    return out


def list_skills() -> list:
    """合并两层清单 → [{name, description, source, path, mtime, size, shadowed}],
    按名排序, 同名 user 在前(repo 那条 shadowed=True, 仍保留供技能页显示)."""
    by_name = {}
    for source in ("repo", "user"):
        for name, meta, path, mtime, size in _scan(source):
            by_name.setdefault(name, []).append(
                {"name": name,
                 "description": (meta.get("description") or "")[:DESC_CAP],
                 "source": source, "path": path, "mtime": mtime, "size": size,
                 "shadowed": False})
    out = []
    for name in sorted(by_name):
        rows = by_name[name]
        user_rows = [r for r in rows if r["source"] == "user"]
        for r in rows:
            if r["source"] == "repo":
                r["shadowed"] = bool(user_rows)
        out.extend(user_rows + [r for r in rows if r["source"] == "repo"])
    return out


def read_skill(name: str) -> tuple:
    """读一份技能正文 → (body, source). 同名 user 层优先; 没有/名字非法 → ("", "").
    超 CONTENT_CAP 截断加标记."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        return "", ""
    for source in ("user", "repo"):
        try:
            with open(os.path.join(_layer_dir(source), name + ".md"),
                      encoding="utf-8", errors="replace") as f:
                meta, body, err = parse_frontmatter(f.read())
        except OSError:
            continue
        if err or (meta.get("name") or "") != name:
            continue
        if len(body) > CONTENT_CAP:
            body = body[:CONTENT_CAP] + "\n…(该技能超 %d 字符已截断)" % CONTENT_CAP
        return body, source
    return "", ""


def write_skill(name: str, description: str, body: str, source: str) -> tuple:
    """新建/覆写一份技能 → (ok, err). 校验 name/description/body/source;
    覆写轮转 .bak(与 memory 同规矩: os.replace 失败改拷贝兜底, 绝不裸覆写)."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return False, "name 只许小写字母/数字/连字符(如 three-piece-suite)"
    desc = (description or "").strip()
    if not desc:
        return False, "description 不能为空(一句话用途)"
    if "\n" in desc:
        return False, "description 必须是一行"
    if not (body or "").strip():
        return False, "正文不能为空"
    if source not in ("repo", "user"):
        return False, "source 只许 repo/user"
    content = "---\nname: %s\ndescription: %s\n---\n%s" % (name, desc, body.strip())
    with _WRITE_LOCK:
        d = _layer_dir(source)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name + ".md")
        if os.path.exists(path):                # 覆写留底
            bak = path + ".bak"
            n = 1
            while os.path.exists(bak):
                n += 1
                bak = "%s.bak-%d" % (path, n)
            try:
                os.replace(path, bak)
            except OSError:
                try:        # 改名被打开句柄锁住 → 拷贝留底(读共享下可成)
                    with open(path, "rb") as src, open(bak, "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return True, ""


def delete_skill(name: str, source: str) -> tuple:
    """删一份技能 → (ok, err). 不真删: 移入 <层>/.trash/(同名加序号), 可找回."""
    name = (name or "").strip()
    if not name or source not in ("repo", "user"):
        return False, "名字/source 无效"
    with _WRITE_LOCK:
        d = _layer_dir(source)
        p = os.path.join(d, name + ".md")
        if not os.path.isfile(p):
            return False, "没有这个技能"
        trash = os.path.join(d, ".trash")
        os.makedirs(trash, exist_ok=True)
        final, tp = name + ".md", os.path.join(trash, name + ".md")
        n = 1
        while os.path.exists(tp):
            n += 1
            final = "%s-%d.md" % (name, n)
            tp = os.path.join(trash, final)
        try:
            os.replace(p, tp)
        except OSError:
            try:            # 拷贝兜底(同 write_skill), 成功后删原件
                with open(p, "rb") as src, open(tp, "wb") as dst:
                    dst.write(src.read())
                os.remove(p)
            except OSError:
                return False, "移动失败"
    return True, ""


def fmt_mtime(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))

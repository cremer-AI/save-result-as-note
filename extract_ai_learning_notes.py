#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 WorkBuddy 任务空间里的历史会话，按"主题分组"导出笔记
（save-result-as-note 技能 · 同主题合并 / 新主题另存 / 无价值提问跳过）。

用法：
  python extract_ai_learning_notes.py [dump|export] [project_glob] [notes_dir]

- dump   ：转储各会话问题清单（供人工甄别分组/跳过）
- export ：生成主题分组笔记（默认）
- project_glob：会话目录 glob，如
    r"C:\\Users\\OYuan\\.workbuddy\\projects\\d-work-AI学习"        （单个目录）
    r"C:\\Users\\OYuan\\.workbuddy\\projects\\c-Users-OYuan-WorkBuddy-2026*"（默认工作区）
- notes_dir：笔记输出目录

分组/跳过方案：`_grouping_plan.json`（键=会话文件名）：
  - 值=[ [1,2],[3] ]                      按问题序号分组
  - 值={"groups":[[1,2]], "skip":[3]}     分组 + 跳过序号
  - 值={"skip":[1]}                       仅跳过，其余启发式分组
无方案时用启发式规则兜底。
"""
import json
import os
import re
import glob
import sys
import subprocess
import datetime

DEFAULT_PROJECT_GLOB = r"C:\Users\OYuan\.workbuddy\projects\d-work-AI学习"
DEFAULT_NOTES_DIR = r"C:\Users\OYuan\WorkBuddy\notes\AI学习"
CURRENT_WORKSPACE = "c-Users-OYuan-WorkBuddy-2026-08-21-09-57-44"  # 当前会话，不导出
PLAN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_grouping_plan.json")
RESULT_CAP = 8000          # 单轮回答正文最大字符数
TITLE_MAX = 30             # 笔记标题最大字符数
SLUG_MAX = 40              # 文件名 slug 最大长度

ILLEGAL = re.compile(r'[\\/:*?"<>|]')
SYS_RE = re.compile(r"<system-reminder.*?</system-reminder>", re.DOTALL)
CB_RE = re.compile(r"<cb_summary>.*?</cb_summary>", re.DOTALL)
IMG_PATH_RE = re.compile(r"<image_local_path>.*?</image_local_path>", re.DOTALL)
QUERY_TAG_RE = re.compile(r"</?user_query>", re.IGNORECASE)
NOISE_TOKEN_RE = re.compile(
    r"@(?:image|scene|skill|tool|agent|app|plugin|connector|video|audio|file|doc|sheet)\S*",
    re.IGNORECASE)

REF_MARKERS = ["更正", "重新", "继续", "进阶", "挑战", "还有", "另外",
               "这个", "那个", "之前", "刚才", "上面", "然后", "对了",
               "再帮", "再给我", "再写", "再找", "能不能", "可以吗"]
SHORT_PREFIX = ("那", "再", "然后", "另外", "还有", "对了", "继续")


def sanitize_slug(title: str) -> str:
    s = title.strip()
    s = s.replace(" ", "-").replace("/", "-")
    s = ILLEGAL.sub("", s)
    s = re.sub(r"[^\w一-鿿\-.]", "", s, flags=re.UNICODE)
    s = s.strip("-_.")
    if not s:
        s = "未命名主题"
    return s[:SLUG_MAX]


def extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("type")
                if t in ("input_text", "output_text", "text"):
                    parts.append(p.get("text", "") or "")
                elif "text" in p and isinstance(p["text"], str):
                    parts.append(p["text"])
        return "\n".join(x for x in parts if x)
    return ""


def clean_query(text: str) -> str:
    if not text:
        return ""
    t = SYS_RE.sub("", text)
    t = CB_RE.sub("", t)
    t = IMG_PATH_RE.sub("", t)
    t = QUERY_TAG_RE.sub("", t)
    return t.strip()


def tokenize(text: str):
    text = text.lower()
    tokens = set()
    for run in re.findall(r"[\w\u4e00-\u9fff]+", text):
        tokens.add(run)
        if len(run) >= 2:
            for i in range(len(run) - 1):
                tokens.add(run[i:i + 2])
    return tokens


def is_followup(q: str, prev_q: str, prev_a: str) -> bool:
    ql = q.lower()
    if "@image" in ql or "image#" in ql or ql.startswith("@"):
        return True
    if any(m in q for m in REF_MARKERS):
        return True
    if q.startswith(SHORT_PREFIX):
        return True
    if len(q) <= 15 and re.search(r"[吗呢?？]$", q):
        return True
    qt = tokenize(q)
    pt = tokenize(prev_q) | tokenize(prev_a)
    if pt and qt:
        overlap = len(qt & pt) / len(qt)
        if overlap >= 0.25:
            return True
    return False


def group_rounds_heuristic(rounds):
    groups = []
    cur = []
    prev_q, prev_a = "", ""
    for q, a in rounds:
        if not cur or is_followup(q, prev_q, prev_a):
            cur.append((q, a))
        else:
            groups.append(cur)
            cur = [(q, a)]
        prev_q, prev_a = q, a or prev_a
    if cur:
        groups.append(cur)
    return groups


def parse_rounds(path: str):
    title = None
    cid = None
    first_ts = None
    rounds = []
    cur_q = None
    cur_answers = []

    def flush():
        nonlocal cur_q, cur_answers
        if cur_q is not None:
            ans = "\n\n".join(a for a in cur_answers if a).strip()
            rounds.append((cur_q, ans))
        cur_q = None
        cur_answers = []

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "ai-title" and not title:
                title = o.get("aiTitle")
            if t == "message":
                role = o.get("role")
                ts = o.get("timestamp")
                if ts is not None:
                    ts = str(ts)
                if first_ts is None and ts:
                    first_ts = ts
                if o.get("sessionId") and cid is None:
                    cid = o.get("sessionId")
                txt = extract_text(o.get("content"))
                if role == "user":
                    q = clean_query(txt)
                    if q and len(q) > 1:
                        flush()
                        cur_q = q
                elif role == "assistant":
                    a = clean_query(txt)
                    if a:
                        cur_answers.append(a)
    flush()

    date_str, time_str = "", ""
    if first_ts:
        m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", first_ts)
        if m:
            date_str, time_str = m.group(1), m.group(2)
    if not date_str:
        dt = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        date_str, time_str = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    return title, date_str, time_str, cid, rounds


def make_title(question: str) -> str:
    q = question.strip()
    first_line = q.splitlines()[0].strip() if q else ""
    # 去掉 @image/@scene/@skill 等工具引用、<image_local_path> 标签与引号内路径
    t = IMG_PATH_RE.sub("", first_line)
    t = NOISE_TOKEN_RE.sub("", t)
    t = QUERY_TAG_RE.sub("", t)
    t = re.sub(r'"[^"]*"', "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > TITLE_MAX:
        t = t[:TITLE_MAX] + "…"
    return t or "未命名主题"


def topic_title(group, session_title) -> str:
    title = make_title(group[0][0])
    if not re.search(r"[\u4e00-\u9fff]", title) and session_title:
        title = session_title[:TITLE_MAX]
    return title


def cap(text, limit, note="…（已截断；完整内容见原会话）"):
    if len(text) > limit:
        return text[:limit] + "\n\n" + note
    return text


def build_group_note(session_title, date_str, time_str, gidx, ngrp,
                     group, cid, src_path, notes_label="任务笔记"):
    title = topic_title(group, session_title)
    total_q = len(group)
    tags = ["WorkBuddy", "任务笔记"]

    qa_lines = []
    for i, (q, a) in enumerate(group, 1):
        ans = a or "（该问题未提取到对应回复）"
        ans = cap(ans, RESULT_CAP)
        qa_lines.append(f"### Q{i}：{q}\n\n{ans}")
    qa_body = "\n\n".join(qa_lines)

    md = f"""---
title: {title}
date: {date_str}
time: {time_str}
source: WorkBuddy 任务笔记（{notes_label} · 主题分组版）
tags: [{', '.join(tags)}]
session: {session_title or '未命名任务'}
session_id: {cid or '未知'}
group: {gidx}/{ngrp}
questions: {total_q}
original: {os.path.basename(src_path)}
---

# {title}

> 所属任务：**{session_title or '未命名任务'}**（共 {ngrp} 个主题，本文为第 {gidx} 个，含 {total_q} 轮问答）

## 问答记录
{qa_body}

## 来源与回溯
- 原会话文件：`{src_path}`
- 会话 ID：`{cid or '未知'}`
- 完整内容可在 WorkBuddy 中重新打开该会话查看。

---
*本笔记由 WorkBuddy「save-result-as-note」技能自动生成（历史任务批量导出 · 同主题合并）*
"""
    return md


def load_plan():
    plan = {}
    if os.path.exists(PLAN_PATH):
        try:
            with open(PLAN_PATH, encoding="utf-8") as fh:
                plan = json.load(fh)
        except Exception as e:
            print(f"[警告] 分组方案读取失败，改用启发式：{e}")
    return plan


def collect_files(project_glob):
    files = sorted(glob.glob(os.path.join(project_glob, "*.jsonl")))
    if CURRENT_WORKSPACE:
        files = [f for f in files if CURRENT_WORKSPACE not in f]
    return files


def dump_rounds(files):
    print("=" * 70)
    for f in files:
        try:
            title, date_str, _, cid, rounds = parse_rounds(f)
        except Exception as e:
            print(f"[跳过] {os.path.basename(f)} 解析失败: {e}")
            continue
        print(f"\n会话: {os.path.basename(f)} | {title or '(无标题)'} | "
              f"{date_str} | {len(rounds)} 问")
        for i, (q, a) in enumerate(rounds, 1):
            print(f"  {i}. {q[:90]}")
    print("=" * 70)


def apply_plan(plan, key, rounds):
    """按方案分组（支持 skip）。序号基于原始 rounds；无方案时启发式。
    返回 (groups, skipped_count)。"""
    if key in plan:
        p = plan[key]
        if isinstance(p, dict):
            skip = set(p.get("skip", []))
            spec = p.get("groups")
        else:
            skip = set()
            spec = p
        if spec:
            groups = []
            for idxs in spec:
                g = [rounds[i - 1] for i in idxs
                     if 1 <= i <= len(rounds) and i not in skip]
                if g:
                    groups.append(g)
            return groups, len(skip)
        if skip:
            rounds = [r for i, r in enumerate(rounds, 1) if i not in skip]
            return group_rounds_heuristic(rounds), len(skip)
        return group_rounds_heuristic(rounds), 0
    return group_rounds_heuristic(rounds), 0


def main():
    args = [a for a in sys.argv[1:]]
    mode = "export"
    if args and args[0] in ("dump", "export"):
        mode = args.pop(0)
    project_glob = args.pop(0) if args else DEFAULT_PROJECT_GLOB
    notes_dir = args.pop(0) if args else DEFAULT_NOTES_DIR

    os.makedirs(notes_dir, exist_ok=True)
    files = collect_files(project_glob)
    print(f"找到会话文件：{len(files)} 个（目录：{project_glob}）")

    if mode == "dump":
        dump_rounds(files)
        return

    plan = load_plan()
    removed = 0
    for old in glob.glob(os.path.join(notes_dir, "*.md")):
        try:
            os.remove(old)
            removed += 1
        except OSError:
            try:
                subprocess.run(["rm", "-f", old], check=False)
                removed += 1
            except Exception:
                print(f"[警告] 未能删除旧文件：{old}")
    if removed:
        print(f"已清理旧笔记：{removed} 个")

    used_names = {}
    report = []
    total_skipped = 0
    for f in files:
        try:
            session_title, date_str, time_str, cid, rounds = parse_rounds(f)
        except Exception as e:
            print(f"[跳过] {os.path.basename(f)} 解析失败: {e}")
            continue
        if not rounds:
            print(f"[跳过] {os.path.basename(f)} 未提取到有效问答")
            continue
        key = os.path.basename(f)
        groups, skipped = apply_plan(plan, key, rounds)
        total_skipped += skipped
        ngrp = len(groups)
        for gidx, group in enumerate(groups, 1):
            title = topic_title(group, session_title)
            slug = sanitize_slug(title)
            base = f"{date_str}-{slug}.md"
            if base in used_names:
                used_names[base] += 1
                base = f"{date_str}-{slug}({used_names[base]}).md"
            else:
                used_names[base] = 0
            out_path = os.path.join(notes_dir, base)
            md = build_group_note(session_title, date_str, time_str, gidx,
                                  ngrp, group, cid, f,
                                  notes_label=os.path.basename(os.path.normpath(notes_dir)))
            with open(out_path, "w", encoding="utf-8") as wf:
                wf.write(md)
            report.append((base, title, len(md), session_title, gidx, ngrp))
        status = f"{len(rounds)} 问 → {ngrp} 篇"
        if skipped:
            status += f"（跳过 {skipped} 问）"
        print(f"[会话] {os.path.basename(f)} | {status}")
        for gi, g in enumerate(groups, 1):
            print(f"   主题{gi}: {topic_title(g, session_title)[:40]}（{len(g)} 问）")

    print(f"\n完成：共生成 {len(report)} 篇笔记 -> {notes_dir}（跳过 {total_skipped} 问）")

    with open(os.path.join(notes_dir, "_导出清单.md"), "w", encoding="utf-8") as lf:
        lf.write("# 默认任务空间 笔记导出清单（主题分组版）\n\n")
        lf.write(f"共 **{len(report)}** 篇笔记，来自 {len(files)} 个会话，"
                 f"生成于 {datetime.date.today().isoformat()}。\n\n")
        last_session = None
        for i, (name, ttl, size, st, gidx, ngrp) in enumerate(report, 1):
            key = st or "(未命名)"
            if key != last_session:
                lf.write(f"\n## {key}（{ngrp} 个主题）\n")
                last_session = key
            lf.write(f"{i}. {ttl}  →  `{name}`\n")
    print("已写入导出清单：_导出清单.md")


if __name__ == "__main__":
    main()

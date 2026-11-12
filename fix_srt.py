#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_srt.py — 校订 ASR 字幕：修同音错字、清理尾巴标点、过长句按字数重新断行并分配时间

ASR（Whisper 等）出来的字幕有三个通病，这个脚本专治：
  1. 同音/近音错字（"皮肤疾渴症" -> "皮肤饥渴症"）
  2. 一行挤了整段话，手机竖屏根本读不完
  3. 行尾残留逗号、台标杂音

用法：
    python fix_srt.py input.zh.srt                        # 用默认参数+内置空规则
    python fix_srt.py input.zh.srt -o out.srt --rules rules.json
    python fix_srt.py input.zh.srt --max-chars 18 --dry-run

rules.json 格式（见 rules.example.json）：
    {
      "replace": [["错词", "正词"], ["另一个错词", "正词"]],
      "drop_contains": ["MING PAO", "字幕组"],
      "max_chars": 22,
      "split_min": 8,
      "drop_trailing_comma": true
    }

替换按"源词长度从长到短"执行，避免短词先吃掉长词的一部分。
"""

import argparse
import json
import re
import sys
from pathlib import Path

SENT_END = "。．.！!？?…"
SPLIT_CHARS = "，,。！？!?、；;"


# ---------------- SRT 读写 ----------------

def srt_ts(sec):
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(txt):
    txt = txt.strip().replace(".", ",")
    m = re.match(r"(\d+):(\d+):(\d+),(\d+)", txt) or re.match(r"(\d+):(\d+),(\d+)", txt)
    if not m:
        raise ValueError(f"看不懂的时间码：{txt}")
    g = [int(x) for x in m.groups()]
    if len(g) == 4:
        return g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
    return g[0] * 60 + g[1] + g[2] / 1000


def read_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [l.strip("\ufeff").rstrip() for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        idx = 1 if "-->" in lines[1] or "-->" in lines[0] else 0
        tline = next((l for l in lines if "-->" in l), None)
        if not tline:
            continue
        a, _, b = tline.partition("-->")
        text = "\n".join(lines[lines.index(tline) + 1:]).strip()
        if not text:
            continue
        cues.append({"start": parse_ts(a), "end": parse_ts(b), "text": text})
    return cues


def write_srt(cues, path):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{srt_ts(c['start'])} --> {srt_ts(c['end'])}\n{c['text']}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return path


# ---------------- 校订 ----------------

DEFAULTS = {
    "replace": [],
    "drop_contains": [],
    "max_chars": 22,
    "split_min": 8,
    "drop_trailing_comma": True,
}


def load_rules(path):
    rules = dict(DEFAULTS)
    if not path:
        return rules
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for k, v in data.items():
        if k in rules:
            rules[k] = v
        else:
            print(f"！规则文件里的 {k} 不认识，已忽略")
    # 长词优先，避免 "莫白" 先把 "沈莫白" 拆坏
    rules["replace"] = sorted(rules["replace"], key=lambda p: -len(p[0]))
    return rules


def apply_text_rules(text, rules):
    t = text
    for a, b in rules["replace"]:
        if a and a in t:
            t = t.replace(a, b)
    if rules["drop_trailing_comma"]:
        t = t.rstrip("，,、;； ").strip()
    return t


def should_drop(text, rules):
    up = text.upper()
    return any(k.upper() in up for k in rules["drop_contains"] if k)


def split_long(cue, limit, split_min):
    """超长句按标点断开，时间按字数比例分配（念得长的分到更多时间）。"""
    t = cue["text"].replace("\n", " ")
    if len(t) <= limit:
        return [cue]
    parts, buf = [], ""
    for ch in t:
        buf += ch
        if ch in SPLIT_CHARS and len(buf) >= split_min:
            parts.append(buf.strip("，,、；;"))
            buf = ""
    if buf.strip():
        parts.append(buf.strip("，,。"))
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [cue]
    total = sum(len(p) for p in parts) or 1
    span_all = cue["end"] - cue["start"]
    out, tt = [], cue["start"]
    for p in parts:
        span = span_all * len(p) / total
        out.append({"start": tt, "end": min(tt + span, cue["end"]), "text": p})
        tt += span
    return out


def fix(cues, rules):
    fixed = []
    for c in cues:
        t = apply_text_rules(c["text"], rules)
        if not t or should_drop(t, rules):
            continue
        fixed.append({"start": c["start"], "end": c["end"], "text": t})

    out = []
    for c in fixed:
        out.extend(split_long(c, rules["max_chars"], rules["split_min"]))
    return out


def main():
    ap = argparse.ArgumentParser(description="校订 ASR 字幕：错字、断句、时间重分配")
    ap.add_argument("input", help="输入 .srt")
    ap.add_argument("-o", "--output", help="输出路径，默认 输入名-fix.srt")
    ap.add_argument("--rules", help="规则 JSON，见 rules.example.json")
    ap.add_argument("--max-chars", type=int, default=None, help="单行字数上限（覆盖规则文件）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"找不到 {src}")
    rules = load_rules(args.rules)
    if args.max_chars:
        rules["max_chars"] = args.max_chars

    cues = read_srt(src)
    if not cues:
        sys.exit("没读到字幕条目，确认是标准 SRT")
    out = fix(cues, rules)
    for c in out:
        if c["end"] - c["start"] < 0.4:      # 太短的条播放器容易闪
            c["end"] = c["start"] + 0.4

    print(f"原 {len(cues)} 条 → 校订后 {len(out)} 条"
          f"（替换 {len(rules['replace'])} 条规则，断行上限 {rules['max_chars']} 字）")
    for c in out[:8]:
        print(f'  [{c["start"]:7.2f}] {c["text"]}')
    print("  ...")

    if args.dry_run:
        print("（dry-run，未写文件）")
        return
    dst = Path(args.output) if args.output else src.with_name(src.stem + "-fix.srt")
    write_srt(out, dst)
    print(f"完成：{dst}")


if __name__ == "__main__":
    main()

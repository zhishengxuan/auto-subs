#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_dub.py — 视频"换语种配音"：保留原说话人音色，替换整条对白。

做法和商用译制配音一样，分五步：

  1. budget   算每句台词的时间预算，把超预算的英文导出来，供改写成"短到能说完"的版本
  2. merge    把改写结果合回时间轴，生成配音专用字幕（定稿）
  3. gen      逐句克隆：切出原片这句话的原声当参考音色，用 F5-TTS 合成目标语言
  4. separate 用 Demucs 把原音轨拆成人声/伴奏，人声丢掉（可选，见 --accomp-gain）
  5. render   配音摆回时间线，和伴奏混音，封装成片；可顺手遮掉原片烧死的硬字幕

关键点：**参考音频就是这句话的原声**，所以谁在说就克隆谁，不需要做说话人分离。
九成效果取决于台词长度——英文信息密度比中文低，直译字幕平均要 1.7 倍时长，
硬加速只会变成"机关枪"。所以 step 1/2 的译配压缩不是可选项。

用法示例（竖屏短剧，39 集合集）：

    # 1) 算预算、导出需要改写的行（按 150 行一批）
    python auto_dub.py budget --srt-zh zh.srt --srt-en en.srt --workdir dubwork

    #    改写完的批次放成 dubwork/fix/fix_0.tsv，每行 "行号<TAB>改写后的英文"
    # 2) 合回时间轴
    python auto_dub.py merge --srt-en en.srt --workdir dubwork --out en-dub.srt

    # 3) 生成配音（可断点续跑，已有段会自动跳过）
    python auto_dub.py gen --video in.mp4 --srt-zh zh.srt --srt-en en-dub.srt \
        --workdir dubwork --model-dir models

    # 4) 分离伴奏（可选；想纯人声就把 --accomp-gain 设 0，这步可以跳过）
    python auto_dub.py separate --video in.mp4 --workdir dubwork

    # 5) 混音 + 封装（顺便遮掉原片 y=1252 处高 100px 的中文字幕带，再烧英文字幕）
    python auto_dub.py mix --srt-zh zh.srt --workdir dubwork --accomp-gain 0.8
    python auto_dub.py render --video in.mp4 --workdir dubwork \
        --blur-band 1252:100 --burn-srt en-dub.srt --out out.dub.mp4

依赖（GPU 建议 12GB 以上显存）：
    torch, torchaudio, f5-tts, demucs, soundfile, numpy, imageio-ffmpeg
模型（首次会下，也可以手动放 models/）：
    F5TTS_v1_Base/model_1250000.safetensors + vocab.txt, charactr/vocos-mel-24khz
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 24000            # 全流程统一 24k 单声道，F5-TTS 和 vocos 都是这个采样率
CHARS_PER_SEC = 13.5  # 目标语言每秒钟能自然说出的字符数（英文约 150 词/分）
MIN_BUDGET = 6        # 极短句也要给几个字符，否则改写成空
NFE_STEP = 24         # F5-TTS 采样步数，24 已经够，调高只变慢
SPEED_CAP = 1.25      # 实在塞不下时最多加速到 1.25 倍（atempo，不变调）


# ------------------------------------------------------------------ 通用

def log(msg: str) -> None:
    print(msg, flush=True)


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run_ff(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{r.stderr[-500:]}")
    return r


def probe_size(ff: str, video: Path) -> tuple[int, int]:
    r = subprocess.run([ff, "-hide_banner", "-i", str(video)],
                       capture_output=True, text=True, errors="replace")
    m = re.search(r", (\d{2,5})x(\d{2,5})", r.stderr)
    if not m:
        raise RuntimeError("读不出视频分辨率")
    return int(m.group(1)), int(m.group(2))


def parse_srt(p: Path | str) -> list[tuple[float, float, str]]:
    t = Path(p).read_text(encoding="utf-8-sig")
    blocks = re.findall(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)\s*\n(.*?)(?=\n\n|\Z)",
        t, re.S)
    out = []
    for b in blocks:
        s = int(b[0]) * 3600 + int(b[1]) * 60 + int(b[2]) + int(b[3]) / 1000
        e = int(b[4]) * 3600 + int(b[5]) * 60 + int(b[6]) + int(b[7]) / 1000
        txt = re.sub(r"\s+", " ", b[8].replace("\n", " ")).strip()
        if txt:
            out.append((s, e, txt))
    return out


def write_srt(p: Path | str, cues: list[tuple[float, float, str]]) -> None:
    def fmt(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int(round((s - int(s)) * 1000))
        if ms == 1000:
            s, ms = s + 1, 0
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

    body = "\n".join(f"{n}\n{fmt(s)} --> {fmt(e)}\n{txt}\n"
                     for n, (s, e, txt) in enumerate(cues, 1))
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1:
            raise ValueError(f"{p} 必须是 24k 单声道")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(p: Path, a: np.ndarray) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    a = np.clip(np.asarray(a, dtype=np.float32), -1.0, 1.0)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((a * 32767).astype(np.int16).tobytes())


def rms_trim(a: np.ndarray, thr_db: float = -45.0) -> np.ndarray:
    """掐掉首尾静音。不掐的话摆回时间线会错位半秒。"""
    if len(a) < 400:
        return a
    win = 400
    energy = np.sqrt(np.convolve(a ** 2, np.ones(win) / win, mode="same"))
    idx = np.where(energy > 10 ** (thr_db / 20))[0]
    if len(idx) == 0:
        return a
    return a[max(0, idx[0] - win): min(len(a), idx[-1] + win)]


def resample_linear(a: np.ndarray, ratio: float) -> np.ndarray:
    n = max(64, int(len(a) / ratio))
    return np.interp(np.linspace(0, len(a) - 1, n),
                     np.arange(len(a)), a).astype(np.float32)


def atempo(a: np.ndarray, ratio: float) -> np.ndarray:
    """变速不变调。ratio>1 加速，<1 放慢。失败时退回线性插值。"""
    r = min(2.0, max(0.5, ratio))
    if abs(r - 1.0) < 0.01:
        return a
    p = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
         "-filter:a", f"atempo={r:.4f}",
         "-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"],
        input=(np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes(),
        capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return resample_linear(a, r)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def ensure_full_wav(video: Path, work: Path) -> Path:
    """抽 24k 单声道全片音轨，后面切参考音、分离伴奏都用它。"""
    p = work / "full.wav"
    if p.exists() and p.stat().st_size > 10000:
        return p
    work.mkdir(parents=True, exist_ok=True)
    log("抽取音轨 …")
    run_ff([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR), str(p)])
    return p


# ------------------------------------------------------------------ 1. budget

def cmd_budget(a) -> None:
    zh = parse_srt(a.srt_zh)
    en = parse_srt(a.srt_en)
    if len(zh) != len(en):
        sys.exit(f"中英字幕条数不一致：{len(zh)} vs {len(en)}")

    budgets = []
    for i, (s, _, _) in enumerate(zh):
        slot = (zh[i + 1][0] - s) if i + 1 < len(zh) else 4.0
        budgets.append(max(MIN_BUDGET, int(slot * CHARS_PER_SEC)))

    work = Path(a.workdir)
    (work / "fix").mkdir(parents=True, exist_ok=True)
    (work / "budgets.json").write_text(json.dumps(budgets), encoding="utf-8")

    todo = [(i, budgets[i], en[i][2])
            for i in range(len(en)) if len(en[i][2]) > budgets[i]]
    for bi in range(0, len(todo), a.batch):
        chunk = todo[bi:bi + a.batch]
        p = work / "fix" / f"todo_{bi // a.batch}.tsv"
        p.write_text("\n".join(f"{i}\t{b}\t{t}" for i, b, t in chunk), encoding="utf-8")
        log(f"{p}  {len(chunk)} 行")

    log(f"\n共 {len(en)} 句，其中 {len(todo)} 句超预算（{100*len(todo)/len(en):.0f}%）。")
    log("改写规则：意思不变、口语化、字数压到预算以内；长句优先砍修饰语，短句可 +2 字。")
    log(f"改完存成 {work}/fix/fix_0.tsv（行号<TAB>英文），然后跑 merge。")


# ------------------------------------------------------------------ 2. merge

def cmd_merge(a) -> None:
    work = Path(a.workdir)
    en = parse_srt(a.srt_en)
    bpath = work / "budgets.json"
    budgets = json.loads(bpath.read_text(encoding="utf-8")) if bpath.exists() else []

    fixed: dict[int, str] = {}
    for f in sorted((work / "fix").glob("fix_*.tsv")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                fixed[int(parts[0])] = parts[1].strip()
            except ValueError:
                continue

    merged, over = [], []
    for i, (s, e, orig) in enumerate(en):
        txt = fixed.get(i, orig)
        if budgets and len(txt) > budgets[i] + 3:
            over.append((i, budgets[i], len(txt), txt))
        merged.append((s, e, txt))

    out = Path(a.out) if a.out else work / "en-dub.srt"
    write_srt(out, merged)
    log(f"总 {len(merged)} 句，改写 {len(fixed)} 句，仍超预算 {len(over)} 句 → {out}")
    for i, b, n, t in over[:10]:
        log(f"  [{i}] 预算 {b} 实际 {n}: {t[:60]}")
    if over:
        log("（超一点没关系，gen 会轻微加速；超过 1.25 倍才会互相压。）")


# ------------------------------------------------------------------ 3. gen

def load_f5(model_dir: Path):
    # f5_tts 内部用 torchaudio.load 读参考音频，而 torchaudio 2.x 的 load
    # 依赖 torchcodec（要额外 ffmpeg DLL）。这里整个换成 soundfile。
    import torch
    import torchaudio

    def _sf_load(uri, *a, **k):
        data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T), sr      # (C, T)

    torchaudio.load = _sf_load

    from f5_tts.api import F5TTS
    log("加载 F5-TTS …")
    f5 = F5TTS(
        model="F5TTS_v1_Base",
        ckpt_file=str(model_dir / "F5TTS_v1_Base" / "model_1250000.safetensors"),
        vocab_file=str(model_dir / "F5TTS_v1_Base" / "vocab.txt"),
        vocoder_local_path=str(model_dir / "vocos-mel-24khz"),
    )
    log("就绪。")
    return f5


def make_ref(full: np.ndarray, ref_dir: Path, i: int, s: float, e: float,
             next_start: float) -> Path:
    """切参考音频。F5 需要 1~2 秒以上的参考，太短就向后面的静音区借。"""
    p = ref_dir / f"ref_{i:04d}.wav"
    if p.exists() and p.stat().st_size > 2000:
        return p
    start = int(max(0.0, s - 0.05) * SR)
    end = int(min(len(full) / SR, e + 0.15) * SR)
    borrow = min(2.0, max(0.0, next_start - e - 0.05))
    want = max(0.0, 2.0 - (e - s))
    end += int(min(borrow, want) * SR)
    seg = full[start:max(start + 1, end)]
    if len(seg) < int(1.0 * SR):
        seg = np.concatenate([seg, np.zeros(int(1.2 * SR) - len(seg), dtype=np.float32)])
    write_wav(p, seg)
    return p


def gen_one(f5, ref_path: Path, ref_text: str, text: str, want_sec: float,
            seed: int | None) -> np.ndarray:
    """F5 的 fix_duration 是「参考音频 + 生成音频」的总时长，不是生成时长。
    这里差一个 ref 长度就会把英文挤进 1/3 时间里——上一版"机关枪"的元凶。"""
    with wave.open(str(ref_path), "rb") as w:
        ref_dur = w.getnframes() / w.getframerate()
    wav, sr, _ = f5.infer(
        ref_file=str(ref_path),
        ref_text=ref_text,
        gen_text=text,
        show_info=lambda *x, **k: None,
        nfe_step=NFE_STEP,
        fix_duration=ref_dur + want_sec,
        cross_fade_duration=0.10,
        seed=seed,
    )
    a = np.asarray(wav, dtype=np.float32)
    return a if sr == SR else resample_linear(a, sr / SR)


def cmd_gen(a) -> None:
    work = Path(a.workdir)
    seg_dir, ref_dir = work / "segs", work / "refs"
    seg_dir.mkdir(parents=True, exist_ok=True)
    ref_dir.mkdir(parents=True, exist_ok=True)

    zh = parse_srt(a.srt_zh)
    src = Path(a.srt_en) if a.srt_en else work / "en-dub.srt"
    en = parse_srt(src)
    if len(zh) != len(en):
        sys.exit(f"两条字幕条数不一致：{len(zh)} vs {len(en)}")

    full = read_wav(ensure_full_wav(Path(a.video), work))
    total = len(full) / SR
    log(f"字幕 {len(zh)} 句，原声 {total/60:.1f} 分钟。")

    f5 = load_f5(Path(a.model_dir))
    stats, done, t0 = [], 0, time.time()

    for i, ((s, e, ref_text), (_, _, text)) in enumerate(zip(zh, en)):
        if a.limit is not None and i >= a.limit:
            break
        out = seg_dir / f"seg_{i:04d}.wav"
        if out.exists() and out.stat().st_size > 4000:
            done += 1
            continue
        if not text.strip():
            write_wav(out, np.zeros(int(0.1 * SR), dtype=np.float32))
            continue

        ref = make_ref(full, ref_dir, i, s, e,
                       zh[i + 1][0] if i + 1 < len(zh) else total)
        # 目标时长：按自然语速说这句话需要多久。F5 给足时长会自己放慢，
        # 给不够就会抢拍，所以这里宁长勿短。
        want = max(0.5 + len(text) / CHARS_PER_SEC, e - s)

        # 同一组参数 F5 的节奏会飘（没种子时尤其明显）：锁种子 + 语速超标换种子重试
        best = None
        for attempt in range(3):
            try:
                cand = rms_trim(gen_one(f5, ref, ref_text, text, want, i * 1000 + attempt))
            except Exception as ex:
                log(f"[{i:04d}] 生成失败：{ex}")
                break
            dur = len(cand) / SR
            rate = len(text) / dur if dur > 0.05 else 999
            if best is None or abs(rate - 15.0) < abs(best[0] - 15.0):
                best = (rate, cand)
            if rate <= 17.0:      # 17 字符/秒 ≈ 160 词/分，日常对话上限
                break
            log(f"[{i:04d}] 语速 {rate:.1f} 偏快，换种子重试…")
        if best is None:
            continue

        rate, cand = best
        # 短句最容易跟参考音频的语速跑飞，兜一层减速（不变调，最多慢到 1.5 倍）
        dur = len(cand) / SR
        if dur > 0.05 and len(text) / dur > 17.0:
            cand = atempo(cand, max(15.0 / (len(text) / dur), 1 / 1.5))

        natural = len(cand) / SR
        avail = max(0.5, ((zh[i + 1][0] if i + 1 < len(zh) else total) - s) - 0.03)
        if natural > avail:       # 塞不进自己的时间槽才加速，封顶 1.25
            cand = atempo(cand, min(natural / avail, SPEED_CAP))

        write_wav(out, cand)
        stats.append({"i": i, "natural": round(natural, 2),
                      "final": round(len(cand) / SR, 2), "avail": round(avail, 2),
                      "rate": round(len(text) / natural, 1) if natural > 0.05 else 0})
        done += 1
        if done % 50 == 0:
            el = time.time() - t0
            log(f"进度 {done} 段 · {el/60:.1f} 分钟 · {el/max(1, done)*1000:.0f}ms/段")

    if stats:
        (work / "gen_stats.json").write_text(json.dumps(stats, ensure_ascii=False),
                                             encoding="utf-8")
        rates = sorted(s["rate"] for s in stats)
        med = rates[len(rates) // 2]
        log(f"完成 {done} 段，{(time.time()-t0)/60:.1f} 分钟。"
            f"语速中位 {med:.1f} 字符/秒，最快 {max(rates):.1f}。")


# ------------------------------------------------------------------ 4. separate

def cmd_separate(a) -> None:
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    work = Path(a.workdir)
    full = ensure_full_wav(Path(a.video), work)
    out = work / "accompaniment.wav"

    data, sr = sf.read(str(full), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    # htdemucs 要求立体声输入，单声道复制一份
    wav = torch.from_numpy(data)[None].repeat(2, 1)
    m = get_model("htdemucs").to(a.device).eval()
    if sr != m.samplerate:
        wav = torch.nn.functional.interpolate(
            wav[None], scale_factor=m.samplerate / sr,
            mode="linear", align_corners=False)[0]

    log(f"分离中，{wav.shape[-1]/m.samplerate/60:.1f} 分钟 …")
    with torch.no_grad():
        parts = apply_model(m, wav[None].to(a.device), device=a.device,
                            split=True, overlap=0.25)[0]
    names = m.sources
    acc = sum(parts[names.index(n)] for n in ("drums", "bass", "other"))
    acc = acc.cpu().numpy().mean(axis=0)          # 立体声 → 单声道

    if m.samplerate != SR:
        acc = resample_linear(acc, m.samplerate / SR)
    write_wav(out, acc)
    log(f"伴奏已保存 {out}（原人声已丢弃，只剩音乐/音效）")


# ------------------------------------------------------------------ 5. mix

def cmd_mix(a) -> None:
    work = Path(a.workdir)
    zh = parse_srt(a.srt_zh)
    seg_dir = work / "segs"
    mix_wav = work / "mix.wav"

    bed = None
    gain = a.accomp_gain
    acc_path = work / "accompaniment.wav"
    if gain > 0:
        if acc_path.exists():
            acc = read_wav(acc_path) * gain
            bed = acc
        else:
            log("！没有 accompaniment.wav，跳过伴奏。先跑 separate，或加 --accomp-gain 0")

    total = int(zh[-1][1] * SR) + SR
    if bed is not None:
        total = max(total, len(bed))
    canvas = np.zeros(total, dtype=np.float32)
    if bed is not None:
        canvas[:len(bed)] += bed

    placed, missing = 0, []
    for i, (s, _, _) in enumerate(zh):
        p = seg_dir / f"seg_{i:04d}.wav"
        if not p.exists():
            missing.append(i)
            continue
        seg = rms_trim(read_wav(p))
        start = int(s * SR)
        end = min(total, start + len(seg))
        if end > start:
            canvas[start:end] += seg[:end - start]
            placed += 1

    peak = float(np.abs(canvas).max())
    if peak > 0.98:
        canvas *= 0.98 / peak
    write_wav(mix_wav, canvas)
    log(f"混音完成：{placed} 段 + 伴奏({gain})，{len(canvas)/SR/60:.1f} 分钟 → {mix_wav}")
    if missing:
        log(f"！缺 {len(missing)} 段（例 {missing[:6]}），gen 没跑完？")


# ------------------------------------------------------------------ 6. render

def srt_to_ass(srt: Path, ass: Path, w: int, h: int, font: str,
               size: int, margin_v: int) -> None:
    cues = parse_srt(srt)
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Dub,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,"
        f"0,0,0,0,100,100,0,0,1,3,1,2,60,60,{margin_v},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def ts(t: float) -> str:
        hh = int(t // 3600)
        mm = int(t % 3600 // 60)
        ss = int(t % 60)
        cs = int(round((t - int(t)) * 100)) % 100
        return f"{hh}:{mm:02d}:{ss:02d}.{cs:02d}"

    lines = [f"Dialogue: 0,{ts(s)},{ts(e)},Dub,,0,0,0,,{t.replace(chr(10), ' ')}"
             for s, e, t in cues]
    ass.parent.mkdir(parents=True, exist_ok=True)
    ass.write_text("\n".join(head + lines), encoding="utf-8")
    log(f"字幕样式 {len(cues)} 条 → {ass}")


def cmd_render(a) -> None:
    video = Path(a.video)
    work = Path(a.workdir)
    ff = ffmpeg_exe()
    w, h = probe_size(ff, video)
    audio = Path(a.audio) if a.audio else work / "mix.wav"
    if not audio.exists():
        sys.exit(f"找不到音轨 {audio}，先跑 mix")

    filters = []
    cur = "[0:v]"

    # 遮蔽原片烧死的硬字幕：模糊一条带，比裁画面安全（裁了会切掉构图）
    if a.blur_band:
        top, height = (int(x) for x in a.blur_band.split(":"))
        height = min(height, h - top)
        filters.append(f"[0:v]split=2[base][forblur]")
        filters.append(f"[forblur]crop={w}:{height}:0:{top},"
                       f"boxblur=luma_radius=28:luma_power=2:chroma_radius=14[blurred]")
        filters.append(f"[base][blurred]overlay=0:{top}[v1]")
        cur = "[v1]"
        log(f"模糊带 y={top} 高 {height}（盖住原来的中文字幕）")

    if a.burn_srt:
        ass = work / "burn.ass"
        margin = (h - (int(a.blur_band.split(":")[0]) + int(a.blur_band.split(":")[1])) + 8
                  if a.blur_band else a.burn_margin)
        srt_to_ass(Path(a.burn_srt), ass, w, h, a.burn_font, a.burn_size, margin)
        # Windows 盘符的冒号必须转义，否则 ffmpeg 会把 C: 当滤镜参数分隔
        arg = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        opt = f"filename='{arg}'"
        if a.fonts_dir:
            fd = str(a.fonts_dir).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            opt += f":fontsdir='{fd}'"
        filters.append(f"{cur}ass={opt}[vout]")
        cur = "[vout]"

    out = Path(a.out) if a.out else work / "dubbed.mp4"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-stats",
           "-i", str(video), "-i", str(audio)]
    if filters:
        cmd += ["-filter_complex", ";".join(filters), "-map", cur]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", "1:a",
            "-c:v", a.vcodec, "-preset", a.preset, "-cq", str(a.cq), "-b:v", "0",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    log("编码中 …")
    t0 = time.time()
    run_ff(cmd)
    log(f"完成 {out}（{out.stat().st_size/1e6:.0f} MB，{(time.time()-t0)/60:.1f} 分钟）")


# ------------------------------------------------------------------ CLI

def main() -> None:
    ap = argparse.ArgumentParser(
        description="视频换语种配音：克隆原说话人音色 + 译配压缩 + 字幕替换",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("budget", help="1) 算时间预算，导出需要改写的台词")
    p.add_argument("--srt-zh", required=True)
    p.add_argument("--srt-en", required=True)
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--batch", type=int, default=150)
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("merge", help="2) 把改写结果合回时间轴")
    p.add_argument("--srt-en", required=True)
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--out")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("gen", help="3) 逐句克隆音色生成配音")
    p.add_argument("--video", required=True)
    p.add_argument("--srt-zh", required=True)
    p.add_argument("--srt-en", help="默认 <workdir>/en-dub.srt")
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--model-dir", default="models")
    p.add_argument("--limit", type=int, help="只生成前 N 句，先试听")
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("separate", help="4) 分离伴奏（去掉原人声）")
    p.add_argument("--video", required=True)
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--device", default="cuda")
    p.set_defaults(func=cmd_separate)

    p = sub.add_parser("mix", help="5) 配音摆回时间线并混音")
    p.add_argument("--srt-zh", required=True)
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--accomp-gain", type=float, default=0.0,
                   help="伴奏音量，0 = 纯人声（默认）")
    p.set_defaults(func=cmd_mix)

    p = sub.add_parser("render", help="6) 遮旧字幕 / 烧新字幕 / 封装成片")
    p.add_argument("--video", required=True)
    p.add_argument("--workdir", default="dubwork")
    p.add_argument("--audio", help="默认 <workdir>/mix.wav")
    p.add_argument("--out")
    p.add_argument("--blur-band", help="遮住原硬字幕的模糊带，格式 TOP:HEIGHT，如 1252:100")
    p.add_argument("--burn-srt", help="烧进画面的字幕（一般用配音版 srt）")
    p.add_argument("--burn-font", default="Arial")
    p.add_argument("--burn-size", type=int, default=52)
    p.add_argument("--burn-margin", type=int, default=60)
    p.add_argument("--fonts-dir", help="自定义字体目录（用了特殊字体才需要）")
    p.add_argument("--vcodec", default="hevc_nvenc")
    p.add_argument("--preset", default="p5")
    p.add_argument("--cq", type=int, default=27)
    p.set_defaults(func=cmd_render)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()

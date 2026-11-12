#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_subs.py — 中文视频 → 多语言字幕 → 软字幕 / 硬字幕成品

五个子命令，可以分开用，也可以串起来跑：

    transcribe   视频/音频 -> 中文字幕 <名字>.zh.srt   （faster-whisper，本地跑，不上传视频）
    translate    中文字幕 -> 一种或多种语言的字幕        （LLM / Google / MyMemory）
    mux          多条字幕做成可切换字幕轨（软字幕，画质零损失）
    burn         把字幕压进视频                        （ffmpeg + libass，手机相册能看）
    all          一条龙：transcribe -> translate -> burn（实验性，见 README）

快速开始：

    # 1) 转写（GPU：32 分钟音频约 2 分半）
    python auto_subs.py transcribe "movie.mp4" --model large-v3 --device cuda

    # 2) 翻成英文（OpenAI 兼容接口；不想配 key 可 --engine mymemory）
    set OPENAI_API_KEY=sk-xxx
    set OPENAI_BASE_URL=https://api.deepseek.com/v1
    python auto_subs.py translate "movie.zh.srt" --to en --model deepseek-chat

    # 3) 做成播放器里可切换的字幕轨
    python auto_subs.py mux "movie.mp4" --srt zh=movie.zh.srt --srt en=movie.en.srt --default zh

    # 4) 或者烧成硬字幕（竖屏建议小字号贴底；非拉丁文字要指定 --fontsdir）
    python auto_subs.py burn "movie.mp4" --srt "movie.en.srt" --font-ratio 0.0243 --margin-v 0.0347

首次运行会自动从 hf-mirror 下载 Whisper 模型（内置国内镜像，不用梯子）：
    tiny / base / small(默认) / medium / large-v3
显存够就 large-v3，最准；纯 CPU 用 small 或 medium。

注意：仅供个人观看和学习，不要传播或商用。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 国内网络下载 HuggingFace 模型必须走镜像，必须在 import faster_whisper 之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SENT_END = "。．.！!？?…"
COMMA = "，,、；;：:"

# 语言表：iso 是 ffmpeg/播放器认的三字母代码，label 是字幕轨显示名
LANGS = {
    "zh": {"iso": "chi", "label": "中文",         "name": "Simplified Chinese"},
    "en": {"iso": "eng", "label": "English",      "name": "English"},
    "my": {"iso": "mya", "label": "မြန်မာ",       "name": "Burmese"},
    "th": {"iso": "tha", "label": "ไทย",          "name": "Thai"},
    "vi": {"iso": "vie", "label": "Tiếng Việt",   "name": "Vietnamese"},
    "id": {"iso": "ind", "label": "Bahasa",       "name": "Indonesian"},
    "ms": {"iso": "msa", "label": "Melayu",       "name": "Malay"},
    "km": {"iso": "khm", "label": "ខ្មែរ",          "name": "Khmer"},
    "lo": {"iso": "lao", "label": "ລາວ",           "name": "Lao"},
    "ja": {"iso": "jpn", "label": "日本語",        "name": "Japanese"},
    "ko": {"iso": "kor", "label": "한국어",        "name": "Korean"},
    "es": {"iso": "spa", "label": "Español",      "name": "Spanish"},
    "pt": {"iso": "por", "label": "Português",    "name": "Portuguese"},
    "ru": {"iso": "rus", "label": "Русский",      "name": "Russian"},
    "ar": {"iso": "ara", "label": "العربية",       "name": "Arabic"},
    "fr": {"iso": "fra", "label": "Français",     "name": "French"},
    "de": {"iso": "deu", "label": "Deutsch",      "name": "German"},
    "hi": {"iso": "hin", "label": "हिन्दी",         "name": "Hindi"},
    "tl": {"iso": "fil", "label": "Filipino",     "name": "Filipino"},
}

# 各目标语言的翻译风格说明（喂给模型）
LANG_STYLE = {
    "en": "modern spoken English. Short drama dialogue is dramatic, emotional, fast — prefer punchy over literary. "
          "Keep each line short enough to read in one glance.",
    "my": "colloquial spoken Burmese in Myanmar script (မြန်မာစာ), the register used in Burmese-dubbed dramas. "
          "Use everyday particles (မောင်း/ပါ/တယ်) rather than literary forms. Transliterate Chinese names "
          "phonetically into Myanmar script and keep honorifics natural (e.g. 顾总 → ဂူ မန်နေဂျာ / ဦးဂူ).",
    "th": "natural spoken Thai as used in Thai-dubbed dramas; keep particles (ครับ/ค่ะ/นะ) consistent with the speaker.",
    "vi": "natural spoken Vietnamese with appropriate kinship pronouns (anh/em/chú/mày) matching the relationship.",
    "id": "colloquial Indonesian (bahasa sehari-hari) as used in sinetron dubbing.",
    "ja": "natural spoken Japanese; keep keigo/plain form consistent with the speaker's status.",
    "ko": "natural spoken Korean with speech levels (해요체/해체) matching the relationship.",
    "es": "neutral Latin-American Spanish dubbing register.",
    "pt": "Brazilian Portuguese dubbing register.",
    "ru": "natural spoken Russian, informal where the Chinese is informal.",
    "ar": "Modern Standard Arabic with colloquial flavor for insults.",
    "fr": "natural spoken French, tutoiement/vouvoiement matching the relationship.",
    "de": "natural spoken German, du/Sie matching the relationship.",
    "hi": "colloquial Hindi as used in dubbing, with natural तू/तुम/आप usage.",
    "tl": "colloquial Filipino/Tagalog as used in teleserye dubbing.",
    "km": "colloquial Khmer as used in dubbed dramas.",
    "lo": "colloquial Lao as used in dubbed dramas.",
    "ms": "colloquial Malay as used in drama dubbing.",
}


# ============================================================
#  SRT 读写
# ============================================================

def srt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(t: str) -> float:
    t = t.strip().replace(".", ",")
    hms, _, ms = t.partition(",")
    parts = hms.split(":")
    while len(parts) < 3:
        parts.insert(0, "0")
    h, m, s = (int(float(p)) for p in parts[:3])
    return h * 3600 + m * 60 + s + (int(ms or 0) / 1000.0)


def read_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\r?\n\r?\n+", raw.strip()):
        lines = [l for l in block.splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        idx = 0
        if re.fullmatch(r"\d+", lines[0].strip()):
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        a, _, b = lines[idx].partition("-->")
        text = "\n".join(lines[idx + 1:]).strip()
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


# ============================================================
#  transcribe：Whisper → 中文字幕
# ============================================================

def pick_device(prefer="auto"):
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception:
        n = 0
    if prefer == "cpu":
        return "cpu", "int8"
    if prefer == "cuda":
        return ("cuda", "float16") if n else ("cpu", "int8")
    # auto：有 CUDA 且能真正建起来才用 GPU
    if n:
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
            WhisperModel("tiny", device="cuda", compute_type="float16")
            return "cuda", "float16"
        except Exception as e:
            msg = str(e).splitlines()[0][:120]
            print(f"   GPU 不可用（{msg}），改用 CPU")
    return "cpu", "int8"


def split_long(sent, max_chars=24):
    """把一句话按自然停顿切成不超过 max_chars 的小块。"""
    if len(sent) <= max_chars:
        return [sent]
    parts, buf = [], ""
    for ch in sent:
        buf += ch
        if ch in SENT_END or (ch in COMMA and len(buf) >= max_chars * 0.45):
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    # 仍然过长的再硬切
    out = []
    for p in parts:
        while len(p) > max_chars * 1.6:
            cut = max(p.rfind(c, 0, int(max_chars * 1.2)) for c in COMMA + " ")
            cut = cut if cut > 0 else max_chars
            out.append(p[:cut + 1])
            p = p[cut + 1:]
        if p.strip():
            out.append(p)
    return [x.strip() for x in out if x.strip()]


def extract_audio(ffmpeg, src, dst):
    """统一转成 16kHz 单声道 wav，避开 PyAV 版本差异导致的解码报错。"""
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "wav", str(dst)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not Path(dst).exists():
        sys.exit(f"音频抽取失败：{(r.stderr or '').strip()[-400:]}")
    return dst


def load_wav16k(path):
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, "必须是 16k 单声道"
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def to_simplified(text):
    try:
        from zhconv import convert
        return convert(text, "zh-cn")
    except Exception:
        return text


def enable_cuda_dlls():
    """ctranslate2 需要 cublas64_12.dll / cudnn*9*.dll。
    pip 安装的 nvidia-* 包把 DLL 放在 site-packages/nvidia/*/bin，不在 PATH 里，手动注册。"""
    if sys.platform != "win32":
        return
    try:
        import nvidia
    except Exception:
        return
    base = Path(nvidia.__file__).parent
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        b = base / sub / "bin"
        if b.is_dir():
            try:
                os.add_dll_directory(str(b))
            except Exception:
                pass
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def cmd_transcribe(args):
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        msg = str(e).splitlines()[0]
        sys.exit(
            "无法加载 faster-whisper 的推理库：\n  " + msg + "\n\n"
            "如果是 WinError 4551（智能应用控制拦截未签名 DLL），三条路选一条：\n"
            "  1. 用剪映/CapCut 自动生成中文字幕并导出 SRT，然后跑：\n"
            "     python auto_subs.py translate 剧名.zh.srt --to en\n"
            "  2. 关掉 设置 → 隐私和安全性 → Windows 安全中心 → 应用和浏览器控制 → 智能应用控制\n"
            "     （注意：关掉后无法重新开启，要恢复只有重装系统）\n"
            "  3. 换一台没开智能应用控制的电脑跑 transcribe，只把 SRT 拷回来"
        )

    src = Path(args.input)
    ffmpeg = find_ffmpeg(getattr(args, "ffmpeg", None))
    if not ffmpeg:
        sys.exit("没找到 ffmpeg，无法抽取音轨")
    enable_cuda_dlls()
    tmpwav = Path(tempfile.mkdtemp(prefix="asr_")) / "audio.wav"
    print("抽取音轨…")
    extract_audio(ffmpeg, src, tmpwav)
    audio = load_wav16k(tmpwav)
    dur = len(audio) / 16000
    print(f"音频 {dur/60:.1f} 分钟")

    device, ctype = pick_device(args.device)
    print(f"模型 {args.model} | 设备 {device} ({ctype}) | 语言 {args.language}")
    t0 = time.time()
    model = WhisperModel(args.model, device=device, compute_type=ctype,
                         download_root=args.model_dir or None)

    segments, info = model.transcribe(
        audio,
        language=args.language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,   # 短剧台词跳脱，关掉能减少幻觉
        initial_prompt=args.prompt or None,
    )
    print(f"识别中…（音频时长 {info.duration/60:.1f} 分钟）")

    cues = []
    for seg in segments:
        text = (seg.text or "").strip()
        text = re.sub(r"[（(][^）)]*[）)]", "", text).strip()   # 去掉(音乐)(掌声)这类标记
        if not text:
            continue
        if args.simplified:
            text = to_simplified(text)
        pieces = split_long(text, args.max_chars)
        dur_seg = max(seg.end - seg.start, 0.3)
        # 按字数比例分配时间
        total = sum(len(p) for p in pieces) or 1
        t = seg.start
        for p in pieces:
            span = dur_seg * len(p) / total
            cues.append({"start": t, "end": min(t + span, seg.end), "text": p})
            t += span
        if len(cues) % 40 < len(pieces):
            print(f"\r  已识别 {len(cues)} 条 / 到 {int(seg.end/60)}:{int(seg.end%60):02d}", end="", flush=True)
    print()

    # 首尾贴合实际时长，避免字幕越飘越远
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"]:
            cues[i]["end"] = cues[i + 1]["start"]
    for c in cues:
        if c["end"] - c["start"] < 0.4:
            c["end"] = c["start"] + 0.4

    out = Path(args.output) if args.output else src.with_suffix(".zh.srt")
    write_srt(cues, out)
    print(f"完成：{out}（{len(cues)} 条，用时 {time.time()-t0:.0f}s）")
    return out


# ============================================================
#  translate：中文 SRT → 英文 SRT
# ============================================================

PROMPT_TMPL = """You are a professional subtitle translator for Chinese short-form drama (短剧).
Translate each numbered Chinese line into {lang_name}.

Rules:
- Output EXACTLY one line per input, formatted as: <number><TAB><translation>
- Never merge, split, skip, renumber, or add commentary.
- If the Chinese line is long, compress the meaning instead of adding a second sentence (aim for about {maxc} characters where the script allows).
- Register: {style}
- Keep character names in pinyin or their conventional form (顾总 -> Mr. Gu, 林晚 -> Lin Wan); do not invent different names for the same person across lines.
- Keep kinship/forms of address natural: 嫂子 -> sis-in-law, 你个废物 -> you worthless piece of trash.
- Idioms and curses: translate the force, not the words. 卧槽 -> Holy shit. 我艹 -> Fuck.
- 霸总/赘婿/重生/穿越 genre clichés keep their flavor ("divorce me? you'll regret it").
- Do not translate on-screen text or sound cues unless they are dialogue.
{glossary}"""


def build_prompt(lang, maxc, glossary=""):
    info = LANGS.get(lang, {"name": lang})
    style = LANG_STYLE.get(lang, "natural, colloquial spoken language of a TV drama.")
    return PROMPT_TMPL.format(lang_name=info["name"], maxc=maxc, style=style,
                              glossary=glossary)



def _session(url):
    """本地地址不走系统代理，否则会被 http_proxy 拦掉。"""
    import requests
    s = requests.Session()
    if any(h in url for h in ("127.0.0.1", "localhost", "0.0.0.0")):
        s.trust_env = False
    return s


def read_glossary(path):
    """术语表 txt：每行 中文=译文。返回可直接塞进提示词的一段文本。"""
    if not path:
        return ""
    pairs = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            a, b = ln.split("=", 1)
            pairs.append(f"{a.strip()} => {b.strip()}")
    if not pairs:
        return ""
    return "Fixed glossary (must use these exact renderings):\n" + "\n".join(pairs)


def _call_llm(lines, cfg, idx_offset):
    import requests
    body = "\n".join(f"{idx_offset + i + 1}\t{t}" for i, t in enumerate(lines))
    payload = {
        "model": cfg["model"],
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": cfg["prompt"]},
            {"role": "user", "content": body},
        ],
    }
    url = cfg["base"].rstrip("/") + "/chat/completions"
    r = _session(url).post(url, headers={"Authorization": f"Bearer {cfg['key']}",
                                         "Content-Type": "application/json"},
                           json=payload, timeout=180)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    got = {}
    for ln in content.splitlines():
        m = re.match(r"\s*(\d+)\s*[\t.、\)]\s*(.+)", ln)
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    return [got.get(idx_offset + i + 1, "") for i in range(len(lines))]


def _call_google(lines, cfg, idx_offset):
    import requests
    out = []
    for t in lines:
        try:
            r = requests.get("https://translate.googleapis.com/translate_a/single",
                             params={"client": "gtx", "sl": "zh-CN", "tl": cfg["target"],
                                     "dt": "t", "q": t}, timeout=20)
            data = r.json()
            out.append("".join(x[0] for x in data[0] if x and x[0]))
        except Exception:
            out.append("")
        time.sleep(0.15)
    return out


def _call_mymemory(lines, cfg, idx_offset):
    """MyMemory 免费接口，无需 key。限速很严：逐条请求 + 固定间隔 + 失败退避。

    质量不如 LLM / 谷歌，但胜在谷歌被墙时能翻缅甸语等小语种。
    """
    import requests
    out = []
    for t in lines:
        got = ""
        for attempt in range(3):
            try:
                r = requests.get("https://api.mymemory.translated.net/get",
                                 params={"q": t, "langpair": f"zh-CN|{cfg['target']}"},
                                 timeout=20)
                d = r.json()
                got = (d.get("responseData") or {}).get("translatedText") or ""
                bad = ("MYMEMORY WARNING" in got.upper()
                       or d.get("responseStatus") != 200 or not got.strip())
                if not bad:
                    break
                got = ""
            except Exception:
                got = ""
            time.sleep(1.5 * (attempt + 1))
        out.append(got)
        time.sleep(0.35)
    return out


def translate_texts(texts, caller, cfg, batch):
    results = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        got = caller(chunk, cfg, i)
        # 漏行就单条重试
        for j, g in enumerate(got):
            if not g:
                retry = caller([chunk[j]], cfg, i + j)
                got[j] = retry[0] if retry and retry[0] else chunk[j]
        results.extend(got)
        print(f"\r  翻译 {min(i+batch, len(texts))}/{len(texts)}", end="", flush=True)
    print()
    return results


def cmd_translate(args):
    src = Path(args.input)
    cues = read_srt(src)
    if not cues:
        sys.exit(f"{src} 里没读到字幕条目")

    targets = [t.strip() for t in (args.to or "en").split(",") if t.strip()]
    unknown = [t for t in targets if t not in LANGS]
    if unknown:
        sys.exit(f"不认识的语言代码：{', '.join(unknown)}\n可用：{', '.join(LANGS)}")
    print(f"读到 {len(cues)} 条中文字幕 → {'/'.join(targets)}")

    glossary = read_glossary(args.glossary)
    base_cfg = {"max_chars": args.max_chars}
    if args.engine == "llm":
        key = args.api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            sys.exit("没找到 API key。设环境变量 OPENAI_API_KEY，或用 --engine google 走免费通道")
        caller = _call_llm
        base_cfg.update({
            "base": args.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            "key": key, "model": args.model,
        })
    elif args.engine == "google":
        caller = _call_google
    else:
        caller = _call_mymemory

    texts = [c["text"].replace("\n", " ") for c in cues]
    t0 = time.time()
    outputs = []
    for target in targets:
        cfg = dict(base_cfg, target=target, prompt=build_prompt(target, args.max_chars, glossary))
        if len(targets) > 1:
            print(f"[{target}] {LANGS[target]['label']}")
        results = translate_texts(texts, caller, cfg, args.batch)

        body = [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in cues]
        for c, tr in zip(body, results):
            c["text"] = f"{c['text']}\n{tr}" if args.bilingual else tr

        if args.output and len(targets) == 1:
            out = Path(args.output)
        else:
            stem = src.stem
            for tail in (".zh", ".cn", ".chs"):
                if stem.lower().endswith(tail):
                    stem = stem[: -len(tail)]
                    break
            # 单语：剧名.en.srt   双语：剧名.zh-en.srt
            suffix = f".zh-{target}.srt" if args.bilingual else f".{target}.srt"
            out = src.with_name(stem + suffix)
        write_srt(body, out)
        outputs.append(out)
        print(f"完成：{out}")

    print(f"全部完成，用时 {time.time()-t0:.0f}s")
    return outputs[0] if len(outputs) == 1 else outputs


# ============================================================
#  burn：字幕压进视频
# ============================================================

def find_ffmpeg(ffmpeg=None):
    if ffmpeg and Path(ffmpeg).exists():
        return ffmpeg
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def has_libass(ffmpeg):
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                           capture_output=True, text=True, errors="replace")
        return "--enable-libass" in (r.stdout or "")
    except Exception:
        return False


def probe_size(ffmpeg, video):
    """返回 (宽, 高)，拿不到就按竖屏 1080x1920 猜。"""
    probe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe")))
    if not Path(probe).exists():
        probe = shutil.which("ffprobe")
    if probe:
        try:
            r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height",
                                "-of", "csv=p=0:s=x", str(video)],
                               capture_output=True, text=True, timeout=30)
            w, _, h = (r.stdout or "").strip().partition("x")
            if w.isdigit() and h.isdigit():
                return int(w), int(h)
        except Exception:
            pass
    for a, b in ((1080, 1920), (1920, 1080), (720, 1280)):
        try:
            r = subprocess.run([ffmpeg, "-i", str(video)], capture_output=True,
                               text=True, errors="replace", timeout=30)
            if f"{a}x{b}" in (r.stderr or ""):
                return a, b
        except Exception:
            pass
    return 1080, 1920


def _char_w(ch):
    # 全角字符（CJK、全角标点）按 2 个宽度单位算
    return 2 if ord(ch) > 0x2E7F else 1


def _wrap_units(seg, budget):
    tokens = re.findall(r"\S+\s*", seg)
    lines, cur, curw = [], "", 0
    for tok in tokens:
        tw = sum(_char_w(c) for c in tok)
        if curw + tw > budget and cur:
            lines.append(cur.rstrip())
            cur, curw = "", 0
        if tw > budget:  # 连续中文或超长单词：按字符硬切
            for ch in tok:
                cw = _char_w(ch)
                if curw + cw > budget:
                    lines.append(cur.rstrip())
                    cur, curw = "", 0
                cur += ch
                curw += cw
        else:
            cur += tok
            curw += tw
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def wrap_cue(text, max_chars, is_vertical):
    """字幕换行：按显示宽度切，竖屏更窄。返回带 \\N 的文本。"""
    budget = min(max_chars, 28) if is_vertical else max_chars * 2
    lines = []
    for seg in text.split("\n"):
        seg = seg.strip()
        if seg:
            lines.extend(_wrap_units(seg, budget))
    return "\\N".join(lines)


def cmd_burn(args):
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        sys.exit("没找到 ffmpeg。装一个：winget install Gyan.FFmpeg，或设环境变量 FFMPEG_PATH")
    if not has_libass(ffmpeg):
        print("！这个 ffmpeg 没带 libass，压不了字幕。换 Gyan.dev 的 full build：winget install Gyan.FFmpeg")

    video = Path(args.input)
    srt = Path(args.srt)
    cues = read_srt(srt)
    if not cues:
        sys.exit(f"{srt} 读不到字幕")

    w, h = probe_size(ffmpeg, video)
    vertical = h > w
    # ffmpeg 的 subtitles 滤镜内部按 PlayResY=288 的虚拟画布换算，
    # FontSize/Outline/MarginV 都必须按 288 高度给出，直接给像素会放大数倍。
    ASS_H = 288.0
    scale = h / ASS_H
    font_ass = max(6, int(round(ASS_H * args.font_ratio)))
    outline_ass = max(1, round(font_ass * 0.07))
    margin_ass = max(6, int(round(ASS_H * args.margin_v)))
    font_px = int(font_ass * scale)

    # 不手动折行：libass 的智能换行对中英文都处理得更好，避免孤行标点。
    # 字幕文件放到纯英文临时目录，避免中文路径在 filter 里炸掉
    tmpdir = Path(tempfile.mkdtemp(prefix="subs_"))
    staged = tmpdir / "sub.srt"
    write_srt(cues, staged)
    srt_arg = str(staged).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    style = ",".join([
        f"FontName={args.font}",
        f"FontSize={font_ass}",
        "PrimaryColour=&H00FFFFFF",       # 白字
        "OutlineColour=&H00000000",
        "BorderStyle=1",
        f"Outline={outline_ass}",
        f"Shadow={max(1, outline_ass // 2)}",
        "Alignment=2",                     # 底部居中
        f"MarginV={margin_ass}",
        "MarginL=12",
        "MarginR=12",
        "WrapStyle=0",
    ])
    fonts = ""
    if getattr(args, "fontsdir", None):
        fd = str(Path(args.fontsdir).resolve()).replace("\\", "/").replace(":", "\\:")
        fonts = f":fontsdir='{fd}'"
    vf = f"subtitles=filename='{srt_arg}'{fonts}:force_style='{style}'"

    out = Path(args.output) if args.output else video.with_name(video.stem + ".en.mp4")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
           "-i", str(video), "-vf", vf,
           "-c:v", args.vcodec, "-preset", args.preset,
           "-cq" if "nvenc" in args.vcodec else "-crf", str(args.crf),
           "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(out)]
    print(f"压制中（{w}x{h}，字号 {font_ass}≈{font_px}px，{args.vcodec} crf{args.crf}）… 这一步要跑一会儿")
    rc = subprocess.run(cmd).returncode
    shutil.rmtree(tmpdir, ignore_errors=True)
    if rc != 0:
        sys.exit("ffmpeg 压制失败，看上面的报错")
    print(f"完成：{out}")
    return out


# ============================================================
#  mux：多条字幕作为可切换轨道封装进视频（软字幕）
# ============================================================

def guess_track_lang(path):
    """从文件名猜语言。剧名.zh.srt → zh；剧名.zh-en.srt → (en, ('zh','en'))"""
    stem = Path(path).stem
    parts = [p for p in re.split(r"[._\s]+", stem) if p]
    bi = None
    codes = []
    for p in parts:
        low = p.lower()
        if re.fullmatch(r"[a-z]{2}-[a-z]{2}", low):
            a, b = low.split("-")
            if a in LANGS and b in LANGS:
                bi = (a, b)
                codes = [a]          # 双语轨按"上面那行"的语言标记，中文在上就算中文轨
                continue
        if low in LANGS:
            codes.append(low)
    return (codes[-1] if codes else None), bi


def parse_track_spec(spec):
    """支持 'zh=剧名.zh.srt' 和 '剧名.zh.srt' 两种写法。"""
    if "=" in spec:
        head, _, tail = spec.partition("=")
        if head.strip().lower() in LANGS:
            return head.strip().lower(), Path(tail.strip())
    return None, Path(spec.strip())


def cmd_mux(args):
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        sys.exit("没找到 ffmpeg。设环境变量 FFMPEG_PATH，或 pip install imageio-ffmpeg")

    video = Path(args.input)
    if not video.exists():
        sys.exit(f"找不到视频：{video}")

    tracks = []
    for spec in args.srt:
        # 形如 xx=路径 但 xx 不是已知语言时，给出明确提示，而不是当成路径报"找不到文件"
        if "=" in spec:
            head = spec.split("=", 1)[0].strip().lower()
            if (re.fullmatch(r"[a-z]{2,3}", head) and head not in LANGS
                    and not any(c in head for c in "/\\:")):
                sys.exit(f"不认识的语言代码：{head}\n可用：{', '.join(LANGS)}")
        lang, path = parse_track_spec(spec)
        if not path.exists():
            sys.exit(f"找不到字幕文件：{path}")
        bi = None
        if not lang:
            lang, bi = guess_track_lang(path)
        if not lang:
            sys.exit(f"猜不出 {path.name} 是哪种语言，请写成 语言代码=路径，例如 en=\"{path.name}\"")
        tracks.append({"lang": lang, "path": path, "bi": bi})
    if not tracks:
        sys.exit("至少要给一条字幕：--srt zh=剧名.zh.srt")

    # 可选：先把某条字幕烧进画面，其余仍做成可切换轨道
    video_in = video
    if args.burn:
        burn_lang = args.burn
        hit = [t for t in tracks if t["lang"] == burn_lang]
        if not hit:
            sys.exit(f"--burn {burn_lang} 指定的字幕不在 --srt 列表里")
        tmp = Path(tempfile.mkdtemp(prefix="muxburn_")) / "burned.mp4"
        print(f"先把 {LANGS[burn_lang]['label']} 烧进画面（这一步要重编码，慢）…")
        ns = argparse.Namespace(input=str(video), srt=str(hit[0]["path"]), output=str(tmp),
                                font=args.font, font_ratio=args.font_ratio,
                                margin_v=args.margin_v, vcodec=args.vcodec,
                                preset=args.preset, crf=args.crf, ffmpeg=ffmpeg,
                                max_chars=42, model_dir=None)
        cmd_burn(ns)
        video_in = tmp
        tracks = [t for t in tracks if t is not hit[0]]

    # 输出格式：按扩展名决定字幕编码（mkv 用 srt，mp4 用 mov_text）
    out = Path(args.output) if args.output else video.with_name(video.stem + ".subs.mkv")
    fmt = out.suffix.lower()
    if fmt not in (".mkv", ".mp4", ".m4v"):
        sys.exit("输出请用 .mkv（推荐，兼容性最好）或 .mp4")

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats", "-i", str(video_in)]
    for t in tracks:
        cmd += ["-i", str(t["path"])]
    cmd += ["-map", "0:v:0", "-map", "0:a?"]
    for i in range(len(tracks)):
        cmd += ["-map", f"{i+1}:0"]
    cmd += ["-c:v", "copy", "-c:a", "copy"]
    cmd += ["-c:s", "srt" if fmt == ".mkv" else "mov_text"]

    default_lang = (args.default or (tracks[0]["lang"] if tracks else None))
    if default_lang and not any(t["lang"] == default_lang for t in tracks):
        # 例如 --burn zh 之后中文轨已经烧进画面了，退回第一条
        print(f"！没有 {default_lang} 字幕轨（可能已被 --burn 烧进画面），默认轨改用 {tracks[0]['lang']}")
        default_lang = tracks[0]["lang"]
    # 只把第一条匹配的轨设为默认，否则播放器会看到多条 default
    default_idx = next((i for i, t in enumerate(tracks) if t["lang"] == default_lang), 0)
    for i, t in enumerate(tracks):
        info = LANGS[t["lang"]]
        title = (f"{LANGS[t['bi'][0]]['label']} + {LANGS[t['bi'][1]]['label']}"
                 if t["bi"] else info["label"])
        cmd += [f"-metadata:s:s:{i}", f"language={info['iso']}",
                f"-metadata:s:s:{i}", f"title={title}",
                f"-disposition:s:{i}", "default" if i == default_idx else "0"]
    cmd += [str(out)]

    print(f"封装 {len(tracks)} 条字幕轨到 {out.name}（视频音频直接复制，不重编码）…")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit("封装失败，看上面的报错")

    list_tracks(ffmpeg, out)
    if fmt != ".mkv":
        print("！MP4 的字幕是 mov_text，PotPlayer/VLC 能切，但 Windows 自带播放器和手机相册多半不显示。"
              "要通用性请用 .mkv")
    return out


def list_tracks(ffmpeg, path):
    """打印成品里的音视频与字幕轨，确认语言标记写对了。"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True, errors="replace")
    lines = [l.strip() for l in (r.stderr or "").splitlines() if "Stream #" in l]
    print("成品轨道：")
    for l in lines:
        print("  " + l)
    subs = [l for l in lines if "Subtitle" in l]
    print(f"  共 {len(subs)} 条字幕轨")
    return lines


# ============================================================
#  CLI
# ============================================================

def build_parser():
    ap = argparse.ArgumentParser(description="中文视频 → 多语言字幕 → 软字幕 / 硬字幕成品")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--max-chars", type=int, default=42, help="单行英文字符上限（默认42，竖屏建议18）")
        p.add_argument("--model-dir", default=None, help="Whisper 模型缓存目录")
        p.add_argument("--simplified", dest="simplified", action="store_true", default=True,
                       help="识别结果繁转简（默认开）")
        p.add_argument("--no-simplified", dest="simplified", action="store_false")

    t = sub.add_parser("transcribe", help="视频/音频 → 中文字幕")
    t.add_argument("input")
    t.add_argument("-o", "--output")
    t.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    t.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    t.add_argument("--language", default="zh")
    t.add_argument("--prompt", default="以下是普通话短剧对白。", help="引导词，可放角色名")
    common(t)
    t.set_defaults(func=cmd_transcribe)

    r = sub.add_parser("translate", help="中文字幕 → 一种或多种语言的字幕")
    r.add_argument("input")
    r.add_argument("-o", "--output", help="只翻一种语言时可用；多语言自动按 .语言.srt 命名")
    r.add_argument("--to", default="en", help="目标语言，逗号分隔：en / en,my / en,my,th")
    r.add_argument("--engine", default="llm", choices=["llm", "google", "mymemory"],
                   help="llm=质量最好(需 key)；google=免费但要能连谷歌；mymemory=免费兜底，缅甸语可用")
    r.add_argument("--model", default="gpt-4o-mini", help="LLM 模型名，如 deepseek-chat / qwen-plus")
    r.add_argument("--base-url", default=None, help="OpenAI 兼容接口地址")
    r.add_argument("--api-key", default=None)
    r.add_argument("--glossary", default=None, help="术语表 txt，每行 中文=译文")
    r.add_argument("--batch", type=int, default=25, help="每批送给模型的条数")
    r.add_argument("--bilingual", action="store_true", help="输出中英(中+目标语)双语，一个文件两行")
    common(r)
    r.set_defaults(func=cmd_translate)

    b = sub.add_parser("burn", help="字幕压进视频")
    b.add_argument("input")
    b.add_argument("--srt", required=True)
    b.add_argument("-o", "--output")
    b.add_argument("--font", default="Arial")
    b.add_argument("--fontsdir", default=None,
                   help="字体目录。缅文/泰文/高棉文等非拉丁文字必须指定，否则渲染成方框")
    b.add_argument("--font-ratio", type=float, default=0.045, help="字号占画面高度比例")
    b.add_argument("--margin-v", type=float, default=0.06, help="距底部比例")
    b.add_argument("--vcodec", default="libx264", choices=["libx264", "libx265", "h264_nvenc", "hevc_nvenc"])
    b.add_argument("--preset", default="medium")
    b.add_argument("--crf", type=int, default=20)
    b.add_argument("--ffmpeg", default=None)
    common(b)
    b.set_defaults(func=cmd_burn)

    m = sub.add_parser("mux", help="多条字幕做成可切换字幕轨（软字幕）")
    m.add_argument("input")
    m.add_argument("--srt", action="append", default=[],
                   help="可重复：zh=剧名.zh.srt，或直接给路径（自动从文件名猜语言）")
    m.add_argument("-o", "--output", help="默认 剧名.subs.mkv")
    m.add_argument("--default", default=None, help="默认显示哪条字幕轨（语言代码）")
    m.add_argument("--burn", default=None,
                   help="同时把某条字幕烧进画面（如 --burn zh），其余仍可切换")
    m.add_argument("--font", default="Microsoft YaHei")
    m.add_argument("--font-ratio", type=float, default=0.045)
    m.add_argument("--margin-v", type=float, default=0.06)
    m.add_argument("--vcodec", default="libx264")
    m.add_argument("--preset", default="medium")
    m.add_argument("--crf", type=int, default=20)
    m.add_argument("--ffmpeg", default=None)
    common(m)
    m.set_defaults(func=cmd_mux)

    a = sub.add_parser("all", help="一条龙：转写 → 翻译 → 压制")
    a.add_argument("input")
    a.add_argument("-o", "--output", help="最终视频输出路径")
    a.add_argument("--model", default="small")
    a.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    a.add_argument("--language", default="zh")
    a.add_argument("--prompt", default="以下是普通话短剧对白。")
    a.add_argument("--engine", default="llm", choices=["llm", "google", "mymemory"])
    a.add_argument("--to", default="en")
    a.add_argument("--llm-model", default="gpt-4o-mini")
    a.add_argument("--base-url", default=None)
    a.add_argument("--api-key", default=None)
    a.add_argument("--glossary", default=None)
    a.add_argument("--batch", type=int, default=25)
    a.add_argument("--bilingual", action="store_true")
    a.add_argument("--font", default="Arial")
    a.add_argument("--font-ratio", type=float, default=0.045)
    a.add_argument("--margin-v", type=float, default=0.06)
    a.add_argument("--vcodec", default="libx264")
    a.add_argument("--preset", default="medium")
    a.add_argument("--crf", type=int, default=20)
    a.add_argument("--ffmpeg", default=None)
    common(a)

    def _all(args):
        video = Path(args.input)
        targs = argparse.Namespace(**vars(args))
        targs.output = None                       # 别把最终 mp4 路径当成 srt 路径
        zh = cmd_transcribe(targs)
        tr = argparse.Namespace(**vars(args))
        tr.input = str(zh)
        tr.output = None
        tr.model = args.llm_model
        en = cmd_translate(tr)
        if isinstance(en, (list, tuple)):
            en = en[0]
        bargs = argparse.Namespace(**vars(args))
        bargs.input = str(video)
        bargs.srt = str(en)
        cmd_burn(bargs)

    a.set_defaults(func=_all)
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

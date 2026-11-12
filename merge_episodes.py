#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_episodes.py — 把一整部短剧的多集 MP4 合并成一个长视频

用法（Windows 命令行 / PowerShell 都行）：

    python merge_episodes.py "D:\\video\\某剧名"

    常用参数：
      --out 文件名        指定输出文件名，默认 "<文件夹名>_全集.mp4"
      -o 输出目录         指定输出目录，默认就放在剧集文件夹的上一级
      --mode auto         默认。先无损快速拼接，校验时长不对就自动转码重拼
      --mode copy         只做无损拼接（最快，1 小时的剧几十秒完成）
      --mode normalize    先统一分辨率和编码再拼（最稳，慢一些）
      --height 1080       强制输出分辨率高度（normalize 模式生效）
      --trim-end 3        每集末尾剪掉 3 秒（去片尾推广，仅 normalize 模式生效）
      --chapters          额外生成带章节的 MKV，可跳集
      --dry-run           只打印计划，不实际执行

依赖：ffmpeg。脚本会自动找系统里的 ffmpeg；找不到就用 pip 的 imageio-ffmpeg。
      手动安装：winget install Gyan.FFmpeg   （或 scoop install ffmpeg）

提示：请仅用于个人离线观看，不要传播或商用。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

VIDEO_EXT = {".mp4", ".mkv", ".ts", ".flv", ".mov", ".m4v", ".avi", ".webm"}


# ---------- ffmpeg 定位 ----------

def find_ffmpeg():
    env = os.environ.get("FFMPEG_PATH")  # 想手动指定就设这个环境变量
    if env and Path(env).exists():
        probe = str(Path(env).with_name(Path(env).name.replace("ffmpeg", "ffprobe")))
        return env, (probe if Path(probe).exists() else None)
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p, p.replace("ffmpeg", "ffprobe")
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            probe = str(Path(exe).with_name(Path(exe).name.replace("ffmpeg", "ffprobe")))
            return exe, (probe if Path(probe).exists() else None)
    except Exception:
        pass
    return None, None


# ---------- 探测 ----------

def probe(path, ffprobe):
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        return json.loads(out)
    except Exception:
        return {}


def media_info(path, ffprobe):
    info = probe(path, ffprobe)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    fps = 0.0
    if v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        n, d = v["r_frame_rate"].split("/")
        try:
            fps = round(float(n) / float(d), 3) if float(d) else 0.0
        except ValueError:
            fps = 0.0
    return {
        "w": int(v.get("width") or 0),
        "h": int(v.get("height") or 0),
        "vcodec": v.get("codec_name") or "",
        "acodec": a.get("codec_name") or "",
        "dur": dur,
        "fps": fps,
        "size": os.path.getsize(path),
    }


# ---------- 排序 ----------

def natural_key(p: Path):
    """第2集排在第10集前面；纯数字文件名同理。"""
    nums = re.findall(r"\d+", p.stem)
    return ([int(n) for n in nums] if nums else [0], p.stem.lower())


def collect(folder: Path, pattern: str):
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    if pattern:
        rx = re.compile(pattern)
        files = [p for p in files if rx.search(p.name)]
    return sorted(files, key=natural_key)


# ---------- 拼接 ----------

def esc_concat(path: Path):
    # concat demuxer 里单引号要写成 '\''
    return "file '" + str(path).replace("\\", "/").replace("'", "'\\''") + "'"


def write_concat_list(files, list_path: Path):
    list_path.write_text("\n".join(esc_concat(f) for f in files) + "\n", encoding="utf-8")


def run(cmd, dry=False):
    if dry:
        print("   $ " + " ".join(str(c) for c in cmd))
        return 0
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        print("   ! ffmpeg 报错：\n     " + "\n     ".join(tail))
    return r.returncode


def do_copy(files, out, ffmpeg, tmp: Path, dry=False):
    lst = tmp / "concat.txt"
    write_concat_list(files, lst)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    return run(cmd, dry)


def do_normalize(files, out, ffmpeg, ffprobe, tmp: Path, target_h, fps, trim_end, crf, dry=False):
    infos = [media_info(f, ffprobe) for f in files]
    # 取出现最多的分辨率作为目标，避免个别集数被拉变形
    cand = Counter((i["w"], i["h"]) for i in infos if i["h"] > 0)
    if target_h:
        base_w, base_h = next(((w, h) for (w, h) in cand if h == target_h), (0, target_h))
        if not base_w:
            base_w = int(round(target_h * 9 / 16 / 2)) * 2
        tw, th = base_w - base_w % 2, target_h - target_h % 2
    elif cand:
        tw, th = cand.most_common(1)[0][0]
        tw, th = tw - tw % 2, th - th % 2
    else:
        tw, th = 1280, 720
    fps = fps or 30

    vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}")

    segs = []
    for idx, f in enumerate(files, 1):
        seg = tmp / f"seg_{idx:04d}.ts"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(f)]
        if trim_end and trim_end > 0:
            d = media_info(f, ffprobe)["dur"]
            if d > trim_end + 1:
                cmd += ["-t", f"{d - trim_end:.3f}"]
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-f", "mpegts", str(seg)]
        if run(cmd, dry) != 0:
            return 1
        segs.append(seg)
        print(f"\r   已转码 {idx}/{len(files)}", end="", flush=True)
    print()

    lst = tmp / "concat_norm.txt"
    write_concat_list(segs, lst)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", str(out)]
    return run(cmd, dry)


def make_chapters(files, infos, out_mkv, ffmpeg, dry=False):
    meta = [";FFMETADATA1"]
    t = 0.0
    for f, i in zip(files, infos):
        start = int(t * 1000)
        end = int((t + i["dur"]) * 1000)
        title = f.stem.replace("=", "-").replace("\n", " ")
        meta += ["[CHAPTER]", "TIMEBASE=1/1000",
                 f"START={start}", f"END={end}", f"title={title}"]
        t += i["dur"]
    mfile = out_mkv.with_suffix(".chapters.txt")
    mfile.write_text("\n".join(meta) + "\n", encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(out_mkv.with_suffix(".mp4").name if False else out_mkv),
           "-i", str(mfile), "-map_metadata", "1", "-codec", "copy", str(out_mkv)]
    rc = run(cmd, dry)
    return rc, mfile


def fmt_dur(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description="把短剧多集合并成一个长视频")
    ap.add_argument("folder", help="存放各集视频的文件夹")
    ap.add_argument("--out", help="输出文件名")
    ap.add_argument("-o", "--outdir", help="输出目录")
    ap.add_argument("--pattern", default="", help="只匹配某类文件名，如 ^第")
    ap.add_argument("--mode", choices=["auto", "copy", "normalize"], default="auto")
    ap.add_argument("--height", type=int, default=0, help="强制输出高度（如 1080）")
    ap.add_argument("--fps", type=float, default=0, help="强制输出帧率")
    ap.add_argument("--trim-end", type=float, default=0, help="每集剪掉末尾 N 秒")
    ap.add_argument("--crf", type=int, default=20, help="转码质量，越小越清晰越大（默认20）")
    ap.add_argument("--chapters", action="store_true", help="额外输出带章节的 MKV")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"找不到文件夹：{folder}")


    files = collect(folder, args.pattern)
    if len(files) < 2:
        sys.exit(f"这个文件夹里只找到 {len(files)} 个视频，没法合并。检查一下 --pattern 或文件后缀。")

    ffmpeg, ffprobe = find_ffmpeg()
    if not ffmpeg:
        sys.exit("没找到 ffmpeg。请先执行：winget install Gyan.FFmpeg   或   pip install imageio-ffmpeg")

    infos = [media_info(f, ffprobe) for f in files]
    total = sum(i["dur"] for i in infos)
    total_size = sum(i["size"] for i in infos)

    print(f"文件夹：{folder}")
    print(f"找到 {len(files)} 集，总时长 {fmt_dur(total)}，源文件共 {total_size/1024/1024:.0f} MB")
    print(f"首集：{files[0].name}  ({infos[0]['w']}x{infos[0]['h']}, {infos[0]['vcodec']}/{infos[0]['acodec']})")
    print(f"末集：{files[-1].name}")
    inconsistent = len({(i["w"], i["h"], i["vcodec"], i["acodec"], i["fps"]) for i in infos}) > 1
    if inconsistent:
        print("提示：各集的分辨率/编码不完全一致，无损拼接可能音画不同步，建议 --mode normalize")
    if any(i["dur"] <= 0 for i in infos):
        print("提示：有文件读不出时长，ffprobe 可能缺失，建议改用 normalize 模式")

    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else folder.parent
    outdir.mkdir(parents=True, exist_ok=True)
    outname = args.out or f"{folder.name}_全集.mp4"
    if not outname.lower().endswith(".mp4"):
        outname += ".mp4"
    out = outdir / outname

    mode = args.mode
    if mode == "auto":
        mode = "normalize" if inconsistent else "copy"
    print(f"模式：{mode}   输出：{out}")

    with tempfile.TemporaryDirectory(prefix="merge_") as td:
        tmp = Path(td)
        ok = False
        if mode == "copy":
            ok = do_copy(files, out, ffmpeg, tmp, args.dry_run) == 0
        else:
            ok = do_normalize(files, out, ffmpeg, ffprobe, tmp,
                              args.height, args.fps, args.trim_end, args.crf, args.dry_run) == 0

        if args.dry_run:
            print("（dry-run，未实际执行）")
            return

        # 无损拼接后校验时长，误差超过 2 秒或 0.5% 就退回转码重拼
        if ok and mode == "copy" and ffprobe:
            got = media_info(out, ffprobe)["dur"]
            expected = total - args.trim_end * len(files) if args.trim_end else total
            if got and abs(got - expected) > max(2.0, expected * 0.005):
                print(f"！无损拼接结果时长 {fmt_dur(got)} 与预期 {fmt_dur(expected)} 不符，改用转码重拼…")
                ok = do_normalize(files, out, ffmpeg, ffprobe, tmp,
                                  args.height, args.fps, args.trim_end, args.crf) == 0

    if ok:
        size = out.stat().st_size / 1024 / 1024
        dur = media_info(out, ffprobe)["dur"] if ffprobe else total
        print(f"完成：{out}")
        print(f"     时长 {fmt_dur(dur)}，体积 {size:.0f} MB（{size/1024:.2f} GB）")
        if args.chapters and ffprobe:
            mkv = out.with_suffix(".mkv")
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(out), "-c", "copy", str(mkv)]
            if run(cmd) == 0:
                rc, mfile = make_chapters(files, infos, mkv, ffmpeg)
                if rc == 0:
                    mfile.unlink(missing_ok=True)
                    print(f"     章节版：{mkv}")
    else:
        print("合并失败，看上面的 ffmpeg 报错。可以先试 --mode normalize。")


if __name__ == "__main__":
    main()

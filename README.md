# auto-subs

中文视频转多语言字幕的小工具。识别、校订、翻译、封装、烧字幕都在本地跑，视频和音频不上传到任何服务器。

最早是给自己做竖屏短剧字幕写的，用顺手了就抽成 CLI。五个子命令，能单独用，也能串起来跑。

```
视频 → Whisper 转写 → 错字校订 → 翻译 → 软字幕 MKV（可切轨，画质不变）
                                     └→ 硬字幕 MP4（手机相册能看）
```

## 能做什么

- **本地识别**：faster-whisper 推理，不联网。模型默认从 `hf-mirror.com` 下（脚本里用 `HF_ENDPOINT` 的 `setdefault`，你设了别的镜像不会被覆盖）。
- **有显卡很快**：本机 RTX 5090 上 32 分钟音频 2 分 36 秒跑完（large-v3，float16），约 12 倍实时。纯 CPU 也能跑，但 large-v3 很慢，建议降到 small 或 medium。
- **19 种语言**：zh / en / my / th / vi / id / ms / km / lo / ja / ko / es / pt / ru / ar / fr / de / hi / tl。这是代码里 `LANGS` 表的全部条目，语言标记和 LLM 提示词都按这张表生成。
- **两种成品**：软字幕是视频流直接 copy，不重编码；硬字幕用 libass 烧进画面，任何播放器都显示。
- **竖屏友好**：字号、边距按画面高度算比例（见下方 `--font-ratio`），能避开视频自带的字幕。
- **非拉丁文字可用**：缅文、泰文、高棉文给 `--fontsdir` 指个字体目录就能正常渲染，否则会掉成方框。

## 环境要求

只在这个组合上完整验证过：

| 项 | 实际用的版本 |
|---|---|
| 系统 | Windows 11 |
| Python | 3.13.12 |
| ffmpeg | imageio-ffmpeg 自带的 7.x；也用 Gyan.dev full build 试过 |
| 显卡 | RTX 5090，驱动自带 CUDA 12.x；cuBLAS / cuDNN 由 pip 包提供 |

代码没用到 3.9 之后的语法，理论上 3.9+ 都能跑；Linux / macOS 我没测过（脚本里有 `sys.platform` 的 Windows 分支，其他系统走的是通用路径）。

- ffmpeg 查找顺序：`FFMPEG_PATH` 环境变量 → 系统 PATH → pip 包 imageio-ffmpeg 自带的那个。
- 硬字幕要求 ffmpeg 编了 libass，`ffmpeg -filters | grep subtitles` 有输出就行。
- 没有 NVIDIA 显卡也能用，识别走 CPU，编码走 libx264，只是慢。

## 安装

```bash
git clone https://github.com/zhishengxuan/auto-subs.git
cd auto-subs
pip install -r requirements.txt
```

Windows + NVIDIA 如果报 `Library cublas64_12.dll is not found`，补两个包：

```bash
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9"
```

`enable_cuda_dlls()` 会遍历 `site-packages/nvidia/{cublas,cudnn,cuda_runtime,cuda_nvrtc}/bin` 并逐个 `os.add_dll_directory`，同时追加到 PATH。不这么做，CTranslate2 加载 DLL 时会失败。

## 快速开始

```bash
# 1) 转写：视频 → 中文字幕（首次运行自动下模型，large-v3 约 3GB）
python auto_subs.py transcribe movie.mp4 --model large-v3 --device cuda --prompt "以下是普通话短剧对白。"

# 2) 校订：修同音错字 + 按 22 字断行
python fix_srt.py movie.zh.srt --rules rules.example.json -o movie.zh-fix.srt

# 3) 翻译：一次出英文和缅甸语两个文件
set OPENAI_API_KEY=sk-xxx
set OPENAI_BASE_URL=https://api.deepseek.com/v1
python auto_subs.py translate movie.zh-fix.srt --to en,my --model deepseek-chat

# 4a) 软字幕：视频音频直接复制，字幕做成可切换的轨道
python auto_subs.py mux movie.mp4 --srt zh=movie.zh-fix.srt --srt en=movie.en.srt --srt my=movie.my.srt --default zh

# 4b) 或者硬字幕：烧进画面（竖屏建议小字号贴底）
python auto_subs.py burn movie.mp4 --srt movie.en.srt --font-ratio 0.0243 --margin-v 0.0347
```

## 子命令

五个子命令共用四个参数：`--max-chars`、`--model-dir`、`--simplified` / `--no-simplified`。其中 `--max-chars` 在 `transcribe` 里是**中文字符**上限，在 `translate` 里是**译文每行英文字符**上限，两者默认值都是 42，含义不同。

### `transcribe` — 语音转字幕

| 参数 | 默认 | 说明 |
|---|---|---|
| `input` | — | 视频或音频文件。有音频流就行，容器不限 |
| `-o` | `<名字>.zh.srt` | 输出路径 |
| `--model` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `--device` | `auto` | `auto` / `cpu` / `cuda` |
| `--language` | `zh` | 源语言。非中文要显式给，否则识别按中文解码会乱 |
| `--prompt` | `以下是普通话短剧对白。` | 引导词，可写角色名和剧种，能明显减少错字和繁体输出 |
| `--max-chars` | `42` | 中文字幕单行字数上限，超了在标点处切分。竖屏建议 18–22 |
| `--model-dir` | HF 缓存目录 | 模型缓存位置 |
| `--no-simplified` | 关 | 关掉繁转简（默认开，用 `zhconv`） |

识别前会先用 ffmpeg 抽 16kHz 单声道 wav，再以 numpy 数组喂给模型，具体原因见「踩过的坑」第 1 条。

### `translate` — 翻译字幕

| 参数 | 默认 | 说明 |
|---|---|---|
| `input` | — | 中文字幕 `.srt` |
| `-o` | 自动命名 | 只在单语言时可用；多语言按 `<名字>.<语言>.srt` 输出 |
| `--to` | `en` | 目标语言，逗号分隔：`en` / `en,my,th`。不认识的代码会直接报错并列出可用值 |
| `--engine` | `llm` | `llm`（质量最好，需 key）/ `google`（免费，要能连谷歌）/ `mymemory`（免费兜底，缅甸语可用） |
| `--model` | `gpt-4o-mini` | 任意 OpenAI 兼容接口的模型名，如 `deepseek-chat`、`qwen-plus` |
| `--base-url` | `OPENAI_BASE_URL` 或官方地址 | 接口地址 |
| `--api-key` | `OPENAI_API_KEY` | 也可以走环境变量 |
| `--glossary` | 无 | 术语表 txt，每行 `中文=译文`，锁定人名地名 |
| `--batch` | `25` | 每批送多少条给模型 |
| `--bilingual` | 关 | 输出中 + 目标语双语，一个文件里两行 |
| `--max-chars` | `42` | 提示词里限制译文单行长度 |

翻译结果是**逐条对齐**的：按批送出去，回来后检查有没有空行，空的单独重试一次，还失败就保留原文，因此条数始终和源字幕一致，不会整段错位。

### `mux` — 软字幕封装

```bash
python auto_subs.py mux movie.mp4 --srt zh=a.zh.srt --srt en=a.en.srt --default zh -o out.mkv
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--srt` | 必填，可重复 | 格式 `语言=文件`，也接受裸路径（自动猜语言） |
| `--default` | 第一条 | 哪条轨默认显示 |
| `--burn` | 无 | 把某条语言烧进画面，其余轨道照样可切 |
| `--font` / `--font-ratio` / `--margin-v` | `Microsoft YaHei` / `0.045` / `0.06` | 仅 `--burn` 时生效 |
| `--vcodec` / `--preset` / `--crf` | `libx264` / `medium` / `20` | 仅 `--burn` 时生效 |

- 视频音频走 `-c copy`，不重编码，几十秒完事。
- 每个字幕流都写 language 和 title 元数据，播放器字幕菜单里能看到「中文 / English / မြန်မာ」。
- `-disposition` 只给第一条匹配的语言设 `default`，避免播放器同时显示多条字幕（这个坑见「踩过的坑」第 5 条）。
- 输出建议 `.mkv`。`.mp4` 的字幕是 mov_text，Windows 自带播放器和手机相册多半不显示。

### `burn` — 硬字幕

| 参数 | 默认 | 说明 |
|---|---|---|
| `--srt` | 必填 | 要烧的字幕，可以多行，按顺序叠放 |
| `--font` | `Arial` | 字体名。中文建议 `Microsoft YaHei` |
| `--fontsdir` | 无 | 字体目录。缅文、泰文、高棉文必须给，否则全是方框 |
| `--font-ratio` | `0.045` | 字号占画面高度比例。1922 高的竖屏用 0.024 约等于 46px |
| `--margin-v` | `0.06` | 距底部比例。贴底用 0.035 |
| `--vcodec` | `libx264` | `libx264` / `libx265` / `h264_nvenc` / `hevc_nvenc` |
| `--preset` / `--crf` | `medium` / `20` | 选 NVENC 时 `-crf` 会自动换成 `-cq` |

### `all` — 一条龙（实验性）

```bash
python auto_subs.py all movie.mp4 --to en --engine llm --crf 28 --vcodec hevc_nvenc
```

串起 `transcribe → translate → burn`，参数是前三个命令的子集，`--llm-model` 用来指定翻译模型。**注意**：这条命令只做了参数解析的验证，没做过端到端跑通（我实际用的都是分步执行），需要一次出片时建议自己拼命令。它也只支持烧一条字幕，不生成软字幕 MKV。

## 配套脚本

### `fix_srt.py` — ASR 字幕校订

```bash
python fix_srt.py movie.zh.srt --rules my_rules.json --max-chars 22 --dry-run
```

| 参数 | 说明 |
|---|---|
| `input` | 待校订的 `.srt` |
| `-o` | 输出路径，默认 `<输入名>-fix.srt` |
| `--rules` | 规则 JSON，格式见下 |
| `--max-chars` | 单行字数上限，覆盖规则文件里的 `max_chars` |
| `--dry-run` | 只打印统计，不写文件 |

错字规则放在外部 JSON 里（`rules.example.json`），换一部剧只改配置：

```json
{
  "replace": [["皮肤疾渴症", "皮肤饥渴症"], ["捏手捏脚", "蹑手蹑脚"]],
  "drop_contains": ["字幕由", "本字幕仅供"],
  "max_chars": 22,
  "split_min": 8,
  "drop_trailing_comma": true
}
```

- `replace`：按源词长度从长到短执行，避免短词先把长词拆坏；
- `drop_contains`：整条含这些片段的字幕直接丢掉，用来清理片头片尾的声明；
- `split_min`：断句后每块的最小字数，避免切出一个字；
- `drop_trailing_comma`：去掉句尾多余的逗号。

超长句在标点处断开，时间按字数比例分摊。

### `merge_episodes.py` — 分集合并

```bash
python merge_episodes.py "D:\video\某剧" --chapters
```

| 参数 | 说明 |
|---|---|
| `folder` | 存放各集视频的文件夹 |
| `--out` / `-o` | 输出文件名 / 输出目录 |
| `--pattern` | 只匹配某类文件名，如 `^第` |
| `--mode` | `auto`（默认）/ `copy`（强制不重编码）/ `normalize`（强制统一编码） |
| `--height` / `--fps` | 强制输出高度、帧率，仅 `normalize` 时生效 |
| `--trim-end` | 每集剪掉末尾 N 秒，处理片尾重复 |
| `--crf` | 转码质量，默认 20 |
| `--chapters` | 额外输出带章节的 MKV，播放器里能跳集 |
| `--dry-run` | 只列出将要合并的文件和时间 |

`auto` 模式先按原样 concat copy，拼完校验总时长；误差超过 2 秒或 0.5% 就自动退回转码重拼。`copy` 和 `normalize` 是强制走某一条路，不校验。

## 实测性能

单机单次测量，不是基准测试，仅供参考：

| 环节 | 硬件 | 耗时 |
|---|---|---|
| 转写 32 分钟音频（large-v3，float16） | RTX 5090 / CUDA | 2 分 36 秒 |
| 硬字幕全片重编码（hevc_nvenc，cq28） | RTX 5090 | 4 分 12 秒（约 7.8 倍实时） |
| 软字幕封装（copy，145MB 视频 + 3 轨） | 同机 | 约 20 秒 |
| 缅甸语翻译 64 条（MyMemory，含 0.35 秒/条限速） | CPU | 约 2 分钟 |

CPU 上跑 large-v3 的具体耗时我没测出结果——32 核机器跑了十几分钟还没过半，就放弃改用 GPU 了。所以「CPU 也能跑」这句只对 small / medium 有把握。

## 依赖与许可证

代码本身没有复制任何第三方项目的源码，但功能全靠下面这些项目：

| 项目 | 用途 | 许可证（按其上游声明） |
|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 语音识别封装 | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | 推理引擎 | MIT |
| [OpenAI Whisper large-v3](https://github.com/openai/whisper) | 识别模型权重 | MIT |
| [FFmpeg](https://ffmpeg.org/) | 抽音轨、合并、封装、调用 libass | LGPL-2.1+（Gyan 的 full build 因包含 GPL 组件而是 GPL） |
| [libass](https://github.com/libass/libass) | 字幕渲染 | ISC |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | 自带一份 ffmpeg 可执行文件 | BSD-2-Clause |
| [numpy](https://numpy.org/) | 音频数组 | BSD-3-Clause |
| [zhconv](https://github.com/gumblex/zhconv) | 繁转简 | MIT |
| [requests](https://requests.readthedocs.io/) | 翻译接口请求 | Apache-2.0 |
| [Noto Sans Myanmar](https://fonts.google.com/noto/specimen/Noto+Sans+Myanmar) | 缅文字体（需自行下载） | OFL-1.1 |

两点说明：

1. ffmpeg 是**以子进程方式调用**，没有链接进本项目，所以本项目标 MIT 不受其 GPL 影响。但如果你把 GPL 版的 ffmpeg 二进制一起打包分发，那个二进制本身仍受 GPL 约束。
2. 上面的许可证是我写代码时记录的上游声明，真要商用请自己再核对一遍对应版本。

## 几个踩过的坑

都是实际撞上之后记下来的，省得你再查一遍。

1. **faster-whisper 和 PyAV 版本冲突**：新版 PyAV 解 mp4 会抛 `metadata_errors`。做法是先用 ffmpeg 抽成 16kHz 单声道 wav，再以 numpy 数组喂给模型，绕开 PyAV 解码这一环。
2. **Windows 上 GPU 缺 cuBLAS / cuDNN**：报 `cublas64_12.dll not found`。装 `nvidia-cublas-cu12` 和 `nvidia-cudnn-cu12` 之后，还得把这些包的 `bin` 目录加进 DLL 搜索路径，`enable_cuda_dlls()` 做的就是这件事。
3. **libass 的字号单位不是像素**：`subtitles` 滤镜内部用 PlayResY=288 的虚拟画布，`FontSize` 得按 288 给，否则会放大六倍以上。脚本里的换算是 `FontSize = 288 × font_ratio`。
4. **竖屏要避开视频自带的硬字幕**：把帧转成灰度图、统计高亮像素所在的行，就能量出原字幕的 y 范围，再把新字幕放到它下面空白的地方。
5. **同语言的多条字幕轨会被全标成 default**：`-disposition` 只能给第一条匹配的语言设 `default`，否则播放器会同时显示好几条字幕。
6. **缅文渲染成方框**：libass 要求字体里有对应字形，`--fontsdir` 指到放 Noto Sans Myanmar 的目录即可。
7. **Windows 智能应用控制会拦截未签名的推理 DLL**：报 `WinError 4551`。这个开关在「Windows 安全中心 → 应用和浏览器控制」里，关掉才能加载 CTranslate2；它关了就很难再开回来，介意的话换台机器跑 `transcribe`，只把 SRT 拷回来做后面的步骤。

## 目录

```
auto-subs/
├── auto_subs.py           主 CLI：transcribe / translate / mux / burn / all
├── fix_srt.py             ASR 字幕校订（错字 + 断行）
├── merge_episodes.py      分集合并（concat + 章节）
├── rules.example.json     错字规则表模板
├── examples/sample.zh.srt 示例字幕（自己编的对白，可直接跑 fix_srt / translate）
├── requirements.txt
└── LICENSE
```

## 已知限制

**测试范围（先看这条）**

- 只在 Windows 11 + Python 3.13.12 + RTX 5090 上实际跑过，Linux / macOS 没验证；
- 没有单元测试，也没有 CI。手工验证过的只有：各子命令 `--help`、`fix_srt --dry-run`、`mymemory` 通道翻 8 条、`mux` 双轨封装、`burn` 走 NVENC；
- `all` 子命令只验证了参数解析，没有端到端跑过；
- 视频编码格式只试过 H.264 和 HEVC 的 mp4。遇到非常规封装（flv、ts、可变帧率）可能需要先自己转一遍。

**功能上的限制**

- 识别用 Whisper，中文短剧的口语、方言、多人抢话还是会有错字，建议过一遍 `fix_srt.py`；
- `mymemory` 是免费机翻，有每日额度，长片会被限流（脚本按 0.35 秒/条节流并做退避重试），质量也明显低于 LLM；
- 硬字幕必然重编码，画质有轻微损失。想无损就用 `mux`；
- 软字幕的可切换性取决于播放器。PotPlayer / VLC / mpv 都正常，Windows 自带「电影和电视」不认 MKV 内嵌轨。

## 免责声明

这个项目只做本地视频和本地字幕处理，不提供也不引导任何内容获取方式。

请只对自己拥有权利的内容使用，不要传播、上传或商用他人作品。翻译和字幕版权归原作者。

## English

A local subtitle pipeline for Chinese video: faster-whisper transcription → rule-based correction of homophone errors → translation (LLM / Google / MyMemory) → soft-sub MKV (stream copy, no re-encode) or hard-sub MP4 (libass + NVENC).

Nothing is uploaded: audio stays on your machine. 19 languages, sizing tuned for vertical video, and `--fontsdir` for Burmese, Thai and Khmer.

Tested only on Windows 11 + Python 3.13 + RTX 5090. No unit tests, no CI; the `all` subcommand is untested end to end. See the Chinese sections above for the full parameter reference and limitations.

```bash
pip install -r requirements.txt
python auto_subs.py transcribe movie.mp4 --model large-v3 --device cuda
python auto_subs.py translate movie.zh.srt --to en,my --model deepseek-chat
python auto_subs.py mux movie.mp4 --srt zh=movie.zh.srt --srt en=movie.en.srt --default zh
```

MIT License.

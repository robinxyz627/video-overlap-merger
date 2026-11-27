"""
FFmpeg 流式多段视频拼接工具（支持硬切去重、接缝色度渐变融合 & H3松锚色调补偿+尾6帧融合）

核心功能：
1. 第 1 段完整保留，第 2 段起默认重叠/裁剪前 N 帧（默认 22 帧，可设为 1 帧或任意值）。
2. 音频裁剪时长无需手动填写，自动按公式 `裁剪时长(秒) = 裁剪帧数 / 视频帧率` 计算。
3. 支持逐个接缝单独选择拼接方式（支持布尔或字符串模式）：
   - "cut" / False（硬切去重，默认）：保留上段尾部，直接裁掉下段前 N 帧。
   - "blend" / True（色度渐变融合）：在两段重叠的 N 帧区间做逐帧线性交叉溶解（视频 blend + 音频 acrossfade）。
   - "tone_blend"（H3松锚色调补偿+尾6帧融合）：自动采样上段尾4帧与下段松开22帧锚点后的稳定区（第 N+4..N+18 帧），
     通过递推 RGB 仿射变换（colorlevels / lutrgb）将下段松锚后的固有变暗与色偏拉平到上段基准，
     并仅在重叠区的最后 6 帧（0.25秒）做短窗线性融合，既保持前 16 帧 100% 锐度无重影，又彻底消除松锚变暗台阶！
4. 支持按文件名数字自然排序或手动顺序。
5. 全程 FFmpeg 流式处理，优先使用 NVIDIA GPU 硬编（h264_nvenc），失败自动回退 libx264。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".flv"}
DEFAULT_TRIM_FRAMES = 22
DEFAULT_AUDIO_RATE = 44100
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

JunctionMode = str  # "cut" | "blend" | "tone_blend" | "direct"


def normalize_junction_mode(val: Union[bool, str, None], trim_frames: int = 22) -> JunctionMode:
    if val is None:
        return "direct" if trim_frames <= 0 else "cut"
    if isinstance(val, bool):
        if trim_frames <= 0:
            return "direct"
        return "blend" if val else "cut"
    s = str(val).strip().lower()
    if s in ("direct", "0", "concat"):
        return "direct"
    if trim_frames <= 0:
        return "direct"
    if s in ("tone_blend", "tone", "h3", "post_guide"):
        return "tone_blend"
    if s in ("blend", "true", "1", "yes"):
        return "blend"
    return "cut"


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    frame_count: int
    duration: float
    has_audio: bool
    audio_sample_rate: int
    audio_channels: int


def find_executable(name: str) -> str:
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    candidates: List[Path] = []

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir / exe_name, exe_dir / "bin" / exe_name])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mp = Path(meipass)
            candidates.extend([mp / exe_name, mp / "bin" / exe_name])

    script_dir = Path(__file__).resolve().parent
    candidates.extend([script_dir / exe_name, script_dir / "bin" / exe_name])

    for cand in candidates:
        if cand.exists():
            return str(cand)

    found = shutil.which(name)
    if found:
        return found

    fallback = [
        Path(r"F:\comfyUI\ComfyUI_windows_portable\ComfyUI\custom_nodes") / exe_name,
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        winget_root = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            for match in winget_root.rglob(exe_name):
                fallback.append(match)
                break
    for cand in fallback:
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(f"未找到 {exe_name}，请确认 FFmpeg 位于程序同目录或已添加到系统 PATH。")


def natural_stem_key(stem: str) -> List[Tuple[int, object]]:
    parts = re.split(r"(\d+)", stem)
    key: List[Tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def numeric_sort_key(path: Path) -> Tuple[int, List[int], List[Tuple[int, object]]]:
    stem = path.stem
    nums = [int(n) for n in re.findall(r"\d+", stem)]
    has_num_rank = 0 if nums else 1
    return (has_num_rank, nums, natural_stem_key(stem))


def parse_fps(stream: dict) -> Fraction:
    for field in ("r_frame_rate", "avg_frame_rate"):
        raw = stream.get(field)
        if raw and raw != "0/0":
            try:
                frac = Fraction(raw)
                if 1 <= float(frac) <= 240:
                    return frac
            except (ValueError, ZeroDivisionError):
                pass
    return Fraction(24, 1)


def probe_video(path: Path, ffprobe_exe: str) -> VideoInfo:
    cmd = [
        ffprobe_exe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_FLAGS,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 检测失败: {path}\n{res.stderr.strip()}")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v_stream is None:
        raise RuntimeError(f"文件中没有视频流: {path}")

    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = int(v_stream["width"])
    height = int(v_stream["height"])
    fps = parse_fps(v_stream)

    duration = 0.0
    for d_raw in (v_stream.get("duration"), fmt.get("duration")):
        if d_raw:
            try:
                duration = float(d_raw)
                if duration > 0:
                    break
            except ValueError:
                pass

    nb_frames_raw = v_stream.get("nb_frames")
    if nb_frames_raw and str(nb_frames_raw).isdigit():
        frame_count = int(nb_frames_raw)
    elif duration > 0:
        frame_count = max(1, int(round(duration * float(fps))))
    else:
        frame_count = 0

    if duration <= 0 and frame_count > 0:
        duration = frame_count / float(fps)

    has_audio = a_stream is not None
    audio_sr = int(a_stream.get("sample_rate", DEFAULT_AUDIO_RATE)) if a_stream else DEFAULT_AUDIO_RATE
    audio_ch = int(a_stream.get("channels", 2)) if a_stream else 2

    return VideoInfo(
        path=path,
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration=duration,
        has_audio=has_audio,
        audio_sample_rate=audio_sr,
        audio_channels=audio_ch,
    )


def expand_input_paths(raw_items: Sequence[str]) -> List[Path]:
    result: List[Path] = []
    seen = set()
    for raw in raw_items:
        cleaned = raw.strip().strip('"').strip("'")
        if not cleaned:
            continue
        p = Path(cleaned)
        if not p.exists():
            print(f"[警告] 路径不存在，已跳过: {cleaned}")
            continue
        if p.is_dir():
            files = [
                f
                for f in p.iterdir()
                if f.is_file()
                and f.suffix.lower() in VIDEO_EXTS
                and not f.name.startswith("merged_")
            ]
            files.sort(key=numeric_sort_key)
            for f in files:
                rp = f.resolve()
                if rp not in seen:
                    seen.add(rp)
                    result.append(f)
        elif p.is_file():
            if p.suffix.lower() not in VIDEO_EXTS:
                print(f"[警告] 不是常见视频格式，已跳过: {p.name}")
                continue
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                result.append(p)
    return result


def calc_trim_seconds(trim_frames: int, fps: Fraction) -> float:
    """公式: 音频裁剪时长(秒) = 裁剪帧数 / 视频帧率"""
    if trim_frames <= 0 or fps <= 0:
        return 0.0
    return float(Fraction(trim_frames, 1) / fps)


def format_fps(fps: Fraction) -> str:
    val = float(fps)
    if abs(val - round(val)) < 1e-4:
        return f"{int(round(val))}"
    return f"{val:.3f}"


def extract_frames_rgb(
    path: Path,
    start_frame: int,
    max_frames: Optional[int],
    pw: int,
    ph: int,
    ffmpeg_exe: str,
) -> List[bytes]:
    """从视频中流式提取指定范围的帧（RGB24 原始字节），用于 AB 对照预览或色调采样。"""
    vf_parts = []
    if max_frames is not None:
        vf_parts.append(f"trim=start_frame={max(0, start_frame)}:end_frame={max(0, start_frame) + max_frames}")
    elif start_frame > 0:
        vf_parts.append(f"trim=start_frame={start_frame}")
    vf_parts.append("setpts=PTS-STARTPTS")
    vf_parts.append(
        f"scale={pw}:{ph}:force_original_aspect_ratio=decrease,"
        f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    vf_str = ",".join(vf_parts)

    cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        vf_str,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"提取预览帧失败 ({path.name}): {err}")

    frame_bytes = pw * ph * 3
    raw = proc.stdout
    total = len(raw) // frame_bytes
    return [raw[i * frame_bytes : (i + 1) * frame_bytes] for i in range(total)]


def compute_rgb_stats(raw_frames: Sequence[bytes]) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """无 numpy 依赖，快速计算一组小尺寸 RGB24 帧的三通道均值 (mean) 与标准差 (std)，范围 [0, 255]。"""
    if not raw_frames:
        return (128.0, 128.0, 128.0), (40.0, 40.0, 40.0)
    sum_r = sum_g = sum_b = 0.0
    sq_r = sq_g = sq_b = 0.0
    count = 0
    for buf in raw_frames:
        mv = memoryview(buf)
        r_slice = mv[0::3]
        g_slice = mv[1::3]
        b_slice = mv[2::3]
        n = len(r_slice)
        if n == 0:
            continue
        sum_r += sum(r_slice)
        sum_g += sum(g_slice)
        sum_b += sum(b_slice)
        sq_r += sum(x * x for x in r_slice)
        sq_g += sum(x * x for x in g_slice)
        sq_b += sum(x * x for x in b_slice)
        count += n
    if count == 0:
        return (128.0, 128.0, 128.0), (40.0, 40.0, 40.0)
    mr, mg, mb = sum_r / count, sum_g / count, sum_b / count
    sr = max(1e-3, (max(0.0, sq_r / count - mr * mr)) ** 0.5)
    sg = max(1e-3, (max(0.0, sq_g / count - mg * mg)) ** 0.5)
    sb = max(1e-3, (max(0.0, sq_b / count - mb * mb)) ** 0.5)
    return (mr, mg, mb), (sr, sg, sb)


def compute_post_guide_tone_params(
    info_a: VideoInfo,
    info_b: VideoInfo,
    trim_frames: int,
    ffmpeg_exe: str,
    prev_gain: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    prev_bias: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]:
    """
    计算段 B 相对于段 A 尾部的两组 RGB 仿射补偿参数 (gain, bias)：
    1) (ov_gain, ov_bias)：用于重叠区 B[:trim_frames] 对齐 A[-trim_frames:]；
    2) (pg_gain, pg_bias)：用于 B[trim_frames:]（松开 22 帧锚点后的自由生成区），
       对比 A 尾部最后 4 帧（应用 prev_gain/prev_bias 后的基准）与 B 松开锚点后的稳定窗口 B[trim_frames+4 : trim_frames+18]，
       自动抵消 H3 模型在松开锚点后固有的变暗与对比度塌陷！
    """
    sw, sh = 160, 90
    n = max(1, trim_frames)
    start_a = max(0, info_a.frame_count - n)
    raw_a = extract_frames_rgb(info_a.path, start_a, n, sw, sh, ffmpeg_exe)
    # 提取 B 的前 n + 18 帧（既包含重叠区 0..n-1，又包含松开锚点后的稳定窗口 n+4..n+18）
    need_b = min(max(n + 18, n + 4), max(1, info_b.frame_count))
    raw_b = extract_frames_rgb(info_b.path, 0, need_b, sw, sh, ffmpeg_exe)

    raw_a_tail = raw_a[-n:] if len(raw_a) >= n else raw_a
    raw_b_head = raw_b[:n] if len(raw_b) >= n else raw_b

    # 应用上一段累计的色彩变换到 A 的统计量上，实现多段递推不衰减
    ma_ov, sa_ov = compute_rgb_stats(raw_a_tail)
    ma_ov = tuple(ma_ov[c] * prev_gain[c] + prev_bias[c] for c in range(3))
    sa_ov = tuple(sa_ov[c] * prev_gain[c] for c in range(3))

    mb_ov, sb_ov = compute_rgb_stats(raw_b_head)
    ov_gain = tuple(max(0.85, min(1.25, sa_ov[c] / max(1.0, sb_ov[c]))) for c in range(3))
    ov_bias = tuple(max(-45.0, min(45.0, ma_ov[c] - mb_ov[c] * ov_gain[c])) for c in range(3))

    # 松开锚点后的稳定窗口统计 (B[n+4 : n+18] vs A[-4:])
    raw_a_last4 = raw_a_tail[-4:] if len(raw_a_tail) >= 4 else raw_a_tail
    ma_ref, sa_ref = compute_rgb_stats(raw_a_last4)
    ma_ref = tuple(ma_ref[c] * prev_gain[c] + prev_bias[c] for c in range(3))
    sa_ref = tuple(sa_ref[c] * prev_gain[c] for c in range(3))

    if len(raw_b) > n + 6:
        raw_b_post = raw_b[n + 4 : min(len(raw_b), n + 18)]
    elif len(raw_b) > n:
        raw_b_post = raw_b[n:]
    else:
        raw_b_post = raw_b_head[-4:]

    mb_pg, sb_pg = compute_rgb_stats(raw_b_post)
    pg_gain = tuple(max(0.88, min(1.25, sa_ref[c] / max(1.0, sb_pg[c]))) for c in range(3))
    pg_bias = tuple(max(-50.0, min(50.0, ma_ref[c] - mb_pg[c] * pg_gain[c])) for c in range(3))

    return ov_gain, ov_bias, pg_gain, pg_bias


def build_lutrgb_filter(gain: Tuple[float, float, float], bias: Tuple[float, float, float]) -> str:
    """生成 FFmpeg lutrgb 滤镜表达式：val * gain + bias，将图像转换到 gbrp 平面做精确线性色彩托举后再转回 yuv420p。"""
    if all(abs(g - 1.0) < 1e-3 for g in gain) and all(abs(b) < 0.2 for b in bias):
        return "null"
    exprs = []
    for ch_name, g, b in zip(("r", "g", "b"), gain, bias):
        exprs.append(f"{ch_name}='clip(val*{g:.5f}+{b:+.3f},0,255)'")
    return f"format=gbrp,lutrgb={':'.join(exprs)},format=yuv420p"


def extract_first_last_thumbnails(
    path: Path,
    thumb_w: int,
    thumb_h: int,
    ffmpeg_exe: str,
) -> Tuple[Optional[bytes], Optional[bytes]]:
    """快速提取视频的首帧与尾帧 RGB24 缩略图，仅解码首尾极少数帧。"""
    vf = (
        f"scale={thumb_w}:{thumb_h}:force_original_aspect_ratio=decrease,"
        f"pad={thumb_w}:{thumb_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    frame_bytes = thumb_w * thumb_h * 3

    first_cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    p_first = subprocess.run(first_cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
    first_raw = p_first.stdout[:frame_bytes] if len(p_first.stdout) >= frame_bytes else None

    last_raw: Optional[bytes] = None
    for use_sseof in (True, False):
        last_cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error"]
        if use_sseof:
            last_cmd.extend(["-sseof", "-1.5"])
        last_cmd.extend([
            "-i",
            str(path),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ])
        p_last = subprocess.run(last_cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        n_frames = len(p_last.stdout) // frame_bytes
        if n_frames >= 1:
            last_raw = p_last.stdout[(n_frames - 1) * frame_bytes : n_frames * frame_bytes]
            break

    return first_raw, last_raw


def build_filter_graph(
    infos: List[VideoInfo],
    trim_frames: int,
    target_fps: Optional[Fraction],
    blend_junctions: Optional[Sequence[Union[bool, str]]] = None,
    ffmpeg_exe: Optional[str] = None,
    junction_trim_frames: Optional[Sequence[int]] = None,
) -> Tuple[str, bool]:
    n_videos = len(infos)
    n_junctions = max(0, n_videos - 1)
    if junction_trim_frames is None:
        j_frames: List[int] = [max(0, trim_frames)] * n_junctions
    else:
        if len(junction_trim_frames) != n_junctions:
            raise ValueError("junction_trim_frames 长度必须等于 视频数量 - 1")
        j_frames = [max(0, int(x)) for x in junction_trim_frames]

    if blend_junctions is None:
        junctions: List[JunctionMode] = ["direct" if j_frames[j] == 0 else "cut" for j in range(n_junctions)]
    else:
        if len(blend_junctions) != n_junctions:
            raise ValueError("blend_junctions 长度必须等于 视频数量 - 1")
        junctions = [normalize_junction_mode(x, j_frames[j]) for j, x in enumerate(blend_junctions)]
        for j, m in enumerate(junctions):
            if m == "direct":
                j_frames[j] = 0

    ref = infos[0]
    out_w, out_h = ref.width, ref.height
    out_fps = target_fps if target_fps is not None else ref.fps
    use_audio = any(info.has_audio for info in infos)

    # 如果存在 tone_blend 接缝，预先流式采样各段的色彩/曝光补偿参数（耗时仅 ~0.1s）
    seg_pg_tone: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = [
        ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
    ] * n_videos
    seg_ov_tone: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = [
        ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
    ] * n_videos

    if any(m == "tone_blend" and j_frames[j] > 0 for j, m in enumerate(junctions)):
        ff_exe = ffmpeg_exe or find_executable("ffmpeg")
        cur_g = (1.0, 1.0, 1.0)
        cur_b = (0.0, 0.0, 0.0)
        for j, mode in enumerate(junctions):
            if mode == "tone_blend" and j_frames[j] > 0:
                ov_g, ov_b, pg_g, pg_b = compute_post_guide_tone_params(
                    infos[j], infos[j + 1], j_frames[j], ff_exe, prev_gain=cur_g, prev_bias=cur_b
                )
                seg_ov_tone[j + 1] = (ov_g, ov_b)
                seg_pg_tone[j + 1] = (pg_g, pg_b)
                cur_g, cur_b = pg_g, pg_b
            else:
                cur_g = (1.0, 1.0, 1.0)
                cur_b = (0.0, 0.0, 0.0)

    lines: List[str] = []

    # 1. 统一各段视频的分辨率、帧率与像素格式
    norm_frames: List[int] = []
    for idx, info in enumerate(infos):
        v_norm_chain: List[str] = []
        if info.fps != out_fps:
            v_norm_chain.append(f"fps={out_fps.numerator}/{out_fps.denominator}")
            est_len = max(1, int(round(info.duration * float(out_fps))))
        else:
            est_len = info.frame_count
        if info.width != out_w or info.height != out_h:
            v_norm_chain.append(
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2"
            )
        v_norm_chain.append("setsar=1,format=yuv420p")
        lines.append(f"[{idx}:v]" + ",".join(v_norm_chain) + f"[vn{idx}];")
        norm_frames.append(est_len)

    # 2. 为每段切分 head(用于上一段融合) / body(主体) / tail(用于下一段融合)
    body_labels: List[Optional[str]] = [None] * n_videos
    head_labels: List[Optional[str]] = [None] * n_videos
    tail_labels: List[Optional[str]] = [None] * n_videos

    for idx, total_f in enumerate(norm_frames):
        in_frames = j_frames[idx - 1] if idx > 0 else 0
        out_frames = j_frames[idx] if idx < n_videos - 1 else 0
        in_mode = junctions[idx - 1] if idx > 0 else "cut"
        out_mode = junctions[idx] if idx < n_videos - 1 else "cut"

        need_head = in_mode in ("blend", "tone_blend") and in_frames > 0
        need_tail = out_mode in ("blend", "tone_blend") and out_frames > 0

        # 对于 tone_blend：为了保持前 16 帧 100% 锐度无重影，只在重叠区的最后 min(6, in_frames) 帧做线性插值！
        in_blend_len = min(6, in_frames) if in_mode == "tone_blend" else (in_frames if in_mode == "blend" else 0)
        out_blend_len = min(6, out_frames) if out_mode == "tone_blend" else (out_frames if out_mode == "blend" else 0)

        body_start = in_frames if idx > 0 else 0
        body_end = (total_f - out_blend_len) if need_tail else total_f

        if total_f > 0:
            if idx > 0 and in_frames > 0 and total_f <= in_frames:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以裁掉 {in_frames} 帧。"
                )
            if need_head and need_tail and total_f < in_frames + out_blend_len:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以同时与前后段融合。"
                )
            if need_tail and not need_head and total_f < out_blend_len:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以截取尾部 {out_blend_len} 帧做融合。"
                )

        has_body = (total_f == 0) or (body_end > body_start)
        slices: List[Tuple[str, str]] = []

        # 如果本段启用了 tone_blend，对 head 应用 ov_tone（或 pg_tone 过渡），对 body/tail 应用 pg_tone
        pg_lut = build_lutrgb_filter(*seg_pg_tone[idx])
        ov_lut = build_lutrgb_filter(*seg_ov_tone[idx])

        if need_head:
            lbl = f"vh{idx}"
            head_labels[idx] = lbl
            h_start = in_frames - in_blend_len
            h_filt = f"trim=start_frame={h_start}:end_frame={in_frames},setpts=PTS-STARTPTS"
            if in_mode == "tone_blend" and ov_lut != "null":
                h_filt += f",{ov_lut}"
            slices.append((lbl, h_filt))

        if has_body:
            lbl = f"vb{idx}"
            body_labels[idx] = lbl
            if need_tail and total_f > 0:
                b_filt = f"trim=start_frame={body_start}:end_frame={body_end},setpts=PTS-STARTPTS"
            elif body_start > 0:
                b_filt = f"trim=start_frame={body_start},setpts=PTS-STARTPTS"
            else:
                b_filt = "setpts=PTS-STARTPTS"
            if pg_lut != "null":
                b_filt += f",{pg_lut}"
            slices.append((lbl, b_filt))

        if need_tail:
            lbl = f"vt{idx}"
            tail_labels[idx] = lbl
            tail_start = max(0, total_f - out_blend_len)
            t_filt = f"trim=start_frame={tail_start},setpts=PTS-STARTPTS"
            if pg_lut != "null":
                t_filt += f",{pg_lut}"
            slices.append((lbl, t_filt))

        if len(slices) == 1:
            lbl, filt = slices[0]
            lines.append(f"[vn{idx}]{filt}[{lbl}];")
        else:
            split_outs = "".join(f"[vns{idx}_{k}]" for k in range(len(slices)))
            lines.append(f"[vn{idx}]split={len(slices)}{split_outs};")
            for k, (lbl, filt) in enumerate(slices):
                lines.append(f"[vns{idx}_{k}]{filt}[{lbl}];")

    # 3. 组装视频拼接链
    v_pieces: List[str] = []
    if body_labels[0] is not None:
        v_pieces.append(f"[{body_labels[0]}]")
    for j in range(n_videos - 1):
        mode = junctions[j]
        jf = j_frames[j]
        if mode in ("blend", "tone_blend") and jf > 0:
            k = min(6, jf) if mode == "tone_blend" else jf
            denom = k + 1
            t_lbl = tail_labels[j]
            h_lbl = head_labels[j + 1]
            m_lbl = f"vj{j}"
            lines.append(
                f"[{t_lbl}][{h_lbl}]blend=all_expr='A*(1-(N+1)/{denom})+B*((N+1)/{denom})':shortest=1,"
                f"setpts=PTS-STARTPTS[{m_lbl}];"
            )
            v_pieces.append(f"[{m_lbl}]")
        if body_labels[j + 1] is not None:
            v_pieces.append(f"[{body_labels[j + 1]}]")

    if len(v_pieces) == 1:
        lines.append(f"{v_pieces[0]}null[vout]" + (";" if use_audio else ""))
    else:
        lines.append(
            f"{''.join(v_pieces)}concat=n={len(v_pieces)}:v=1:a=0[vout]"
            + (";" if use_audio else "")
        )

    # 4. 组装音频链
    if use_audio:
        a_norm_labels: List[str] = []
        for idx, info in enumerate(infos):
            lbl = f"an{idx}"
            a_norm_labels.append(lbl)
            if info.has_audio:
                lines.append(
                    f"[{idx}:a]asetpts=PTS-STARTPTS,"
                    f"aresample={DEFAULT_AUDIO_RATE}:async=1:first_pts=0,"
                    f"aformat=sample_fmts=fltp:sample_rates={DEFAULT_AUDIO_RATE}:channel_layouts=stereo[{lbl}];"
                )
            else:
                dur = norm_frames[idx] / float(out_fps) if norm_frames[idx] > 0 else info.duration
                lines.append(
                    f"anullsrc=r={DEFAULT_AUDIO_RATE}:cl=stereo:d={max(0.01, dur):.6f}[{lbl}];"
                )

        acc_lbl = a_norm_labels[0]
        for j in range(n_videos - 1):
            next_info = infos[j + 1]
            next_lbl = a_norm_labels[j + 1]
            mode = junctions[j]
            jf = j_frames[j]
            overlap_sec = calc_trim_seconds(jf, out_fps)
            out_lbl = "aout" if j == n_videos - 2 else f"aacc{j}"

            if mode == "blend" and overlap_sec > 0.001:
                lines.append(
                    f"[{acc_lbl}][{next_lbl}]acrossfade=d={overlap_sec:.6f}:c1=tri:c2=tri[{out_lbl}]"
                    + (";" if j < n_videos - 2 else "")
                )
            elif mode == "tone_blend" and overlap_sec > 0.001:
                blend_f = min(6, jf)
                pre_trim_f = max(0, jf - blend_f)
                fade_sec = calc_trim_seconds(blend_f, out_fps)
                if pre_trim_f > 0:
                    pre_sec = calc_trim_seconds(pre_trim_f, out_fps)
                    trimmed_lbl = f"atrim{j + 1}"
                    lines.append(
                        f"[{next_lbl}]atrim=start={pre_sec:.6f},asetpts=PTS-STARTPTS[{trimmed_lbl}];"
                    )
                    seg_b = trimmed_lbl
                else:
                    seg_b = next_lbl
                lines.append(
                    f"[{acc_lbl}][{seg_b}]acrossfade=d={fade_sec:.6f}:c1=tri:c2=tri[{out_lbl}]"
                    + (";" if j < n_videos - 2 else "")
                )
            else:
                if jf > 0:
                    trim_sec = calc_trim_seconds(jf, next_info.fps if next_info.fps == out_fps else out_fps)
                    trimmed_lbl = f"atrim{j + 1}"
                    lines.append(
                        f"[{next_lbl}]atrim=start={trim_sec:.6f},asetpts=PTS-STARTPTS[{trimmed_lbl}];"
                    )
                    seg_b = trimmed_lbl
                else:
                    seg_b = next_lbl
                lines.append(
                    f"[{acc_lbl}][{seg_b}]concat=n=2:v=0:a=1[{out_lbl}]"
                    + (";" if j < n_videos - 2 else "")
                )
            acc_lbl = out_lbl

    return "\n".join(lines), use_audio


def run_ffmpeg_merge(
    infos: List[VideoInfo],
    output_path: Path,
    trim_frames: int,
    target_fps: Optional[Fraction],
    encoder_mode: str,
    ffmpeg_exe: str,
    blend_junctions: Optional[Sequence[Union[bool, str]]] = None,
    log_cb=None,
    junction_trim_frames: Optional[Sequence[int]] = None,
) -> None:
    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)
        else:
            print(msg)

    filter_graph, use_audio = build_filter_graph(
        infos,
        trim_frames,
        target_fps,
        blend_junctions,
        ffmpeg_exe=ffmpeg_exe,
        junction_trim_frames=junction_trim_frames,
    )
    filter_graph_single_line = "".join(line.strip() for line in filter_graph.splitlines())
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def build_cmd(codec: str) -> List[str]:
        cmd = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
        for info in infos:
            cmd.extend(["-i", str(info.path)])
        cmd.extend(["-filter_complex", filter_graph_single_line])
        cmd.extend(["-map", "[vout]"])
        if use_audio:
            cmd.extend(["-map", "[aout]"])

        if codec == "nvenc":
            cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"])

        if use_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        cmd.extend(["-movflags", "+faststart", str(output_path)])
        return cmd

    encoders_to_try = ["nvenc", "x264"] if encoder_mode == "auto" else [encoder_mode]
    last_err = ""
    for idx, enc in enumerate(encoders_to_try):
        log(f"[执行] 正在使用编码器: {'h264_nvenc (GPU加速)' if enc == 'nvenc' else 'libx264 (CPU编码)'} ...")
        proc = subprocess.run(
            build_cmd(enc),
            capture_output=(log_cb is not None),
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=SUBPROCESS_FLAGS,
        )
        if proc.returncode == 0:
            return
        stderr_msg = proc.stderr.strip() if proc.stderr else ""
        last_err = f"编码器 {enc} 返回错误码 {proc.returncode}" + (f": {stderr_msg}" if stderr_msg else "")
        if idx + 1 < len(encoders_to_try):
            log(f"[提示] {last_err}，自动回退到 libx264 重试...")
    raise RuntimeError(f"FFmpeg 合并失败：{last_err}")


def default_output_path(infos: Sequence[VideoInfo]) -> Path:
    first_dir = infos[0].path.parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return first_dir / f"merged_{stamp}.mp4"


def parse_blend_arg(blend_arg: Optional[str], num_videos: int, mode_val: str = "blend") -> List[JunctionMode]:
    junctions: List[JunctionMode] = ["cut"] * max(0, num_videos - 1)
    if not blend_arg:
        return junctions
    raw = blend_arg.strip().lower()
    if raw in ("tone", "tone_blend", "h3"):
        return ["tone_blend"] * len(junctions)
    if raw in ("all", "true", "yes", "blend"):
        return [mode_val] * len(junctions)
    if raw in ("none", "false", "no", "cut"):
        return junctions
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        m = mode_val
        if ":" in token:
            token, m_str = token.split(":", 1)
            m = normalize_junction_mode(m_str)
        if "-" in token:
            a_str, _ = token.split("-", 1)
            idx = int(a_str.strip()) - 1
        else:
            idx = int(token) - 1
        if 0 <= idx < len(junctions):
            junctions[idx] = m
    return junctions


def junction_mode_label(mode: Union[bool, str], trim_frames: int) -> str:
    m = normalize_junction_mode(mode, trim_frames)
    if m == "direct" or trim_frames <= 0:
        return "0帧直接拼接 (不裁剪)"
    if m == "tone_blend":
        return f"⭐H3松锚色调补偿+尾{min(6, trim_frames)}帧融合 ({trim_frames}帧)"
    if m == "blend":
        return f"色度渐变融合 ({trim_frames}帧)"
    return f"硬切去重 (裁前{trim_frames}帧)"


def print_summary(
    infos: List[VideoInfo],
    trim_frames: int,
    sort_label: str,
    encoder_mode: str,
    output_path: Path,
    blend_junctions: Sequence[Union[bool, str]],
) -> None:
    print("\n" + "=" * 78)
    print(f"当前排序方式 : {sort_label}")
    print(f"第2段起重叠  : {trim_frames} 帧（音频时长自动按 公式 = 裁剪帧数 / 帧率 计算）")
    print(f"编码器模式   : {encoder_mode} (auto=优先NVIDIA显卡硬编，失败自动切libx264)")
    print(f"输出文件     : {output_path}")
    print("-" * 78)
    total_out_frames = 0
    for i, info in enumerate(infos):
        fps_str = format_fps(info.fps)
        audio_tag = "有音频" if info.has_audio else "无音频"
        if i == 0 or trim_frames == 0:
            trim_desc = "完整保留（首段）"
            kept = info.frame_count
        else:
            sec = calc_trim_seconds(trim_frames, info.fps)
            mode_str = junction_mode_label(blend_junctions[i - 1], trim_frames)
            trim_desc = f"[{mode_str}] 重叠 {trim_frames} 帧 / {sec:.4f}s"
            kept = max(0, info.frame_count - trim_frames)
        total_out_frames += kept
        print(
            f"  [{i + 1}] {info.path.name}\n"
            f"      {info.width}x{info.height} | {fps_str} fps | {info.frame_count} 帧 ({info.duration:.2f}s) | {audio_tag} -> {trim_desc}"
        )
    ref_fps = float(infos[0].fps)
    est_sec = total_out_frames / ref_fps if ref_fps > 0 else 0.0
    print("-" * 78)
    print(f"预计成片     : {len(infos)} 段共 {total_out_frames} 帧（约 {est_sec:.2f} 秒 @ {format_fps(infos[0].fps)} fps）")
    print("=" * 78)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FFmpeg 低内存多段视频裁剪合并工具")
    parser.add_argument("inputs", nargs="*", help="输入的视频文件或文件夹路径")
    parser.add_argument("-f", "--trim-frames", type=int, default=DEFAULT_TRIM_FRAMES, help=f"重叠/裁剪帧数（默认 {DEFAULT_TRIM_FRAMES}）")
    parser.add_argument("--sort", choices=["auto", "numeric", "manual"], default="auto", help="排序方式")
    parser.add_argument("--fps", type=float, default=None, help="可选目标帧率（默认跟随第1段）")
    parser.add_argument("--encoder", choices=["auto", "nvenc", "x264"], default="auto", help="视频编码器")
    parser.add_argument("--blend", type=str, default=None, help="开启接缝融合模式：all(22帧渐变) / tone(H3松锚色调补偿+尾6帧融合) / 1-2,3-4")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出 MP4 路径")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认直接合并")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    ffmpeg_exe = find_executable("ffmpeg")
    ffprobe_exe = find_executable("ffprobe")

    if args.trim_frames < 0:
        print("[错误] --trim-frames 不能为负数。")
        return 1

    target_fps = Fraction(args.fps).limit_denominator(10000) if args.fps else None
    output_path = Path(args.output) if args.output else None

    if not args.inputs:
        print("请传入视频文件或文件夹路径，或使用图形界面 merge_gui.py / start_video_merger.bat。")
        return 1

    paths = expand_input_paths(args.inputs)
    if len(paths) < 2:
        print(f"[错误] 仅找到 {len(paths)} 个有效视频文件，至少需要 2 个视频。")
        return 1

    infos = [probe_video(p, ffprobe_exe) for p in paths]
    sort_mode = "numeric" if args.sort in ("auto", "numeric") else "manual"
    if sort_mode == "numeric":
        infos = sorted(infos, key=lambda x: numeric_sort_key(x.path))
        sort_label = "文件名数字从小到大"
    else:
        sort_label = "传入顺序"

    blend_junctions = parse_blend_arg(args.blend, len(infos))
    out = output_path or default_output_path(infos)
    print_summary(infos, args.trim_frames, sort_label, args.encoder, out, blend_junctions)

    if not args.yes:
        ans = input("\n按 Enter 开始合并，输入 q 退出 > ").strip().lower()
        if ans in ("q", "quit", "exit"):
            return 0

    start_t = datetime.now()
    run_ffmpeg_merge(infos, out, args.trim_frames, target_fps, args.encoder, ffmpeg_exe, blend_junctions)
    elapsed = (datetime.now() - start_t).total_seconds()
    print(f"\n[完成] 合并成功！耗时 {elapsed:.2f} 秒 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

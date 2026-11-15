"""
FFmpeg 流式多段视频拼接工具（支持硬切去重 & 指定接缝色度渐变融合）

核心功能：
1. 第 1 段完整保留，第 2 段起默认重叠/裁剪前 N 帧（默认 22 帧，可设为 1 帧或任意值）。
2. 音频裁剪时长无需手动填写，自动按公式 `裁剪时长(秒) = 裁剪帧数 / 视频帧率` 计算。
3. 支持逐个接缝单独选择拼接方式：
   - 硬切去重（默认）：保留上段尾部，直接裁掉下段前 N 帧。
   - 色度渐变融合：在两段重叠的 N 帧区间做逐帧线性交叉溶解（视频 blend + 音频 acrossfade），
     总帧数和总时长与硬切完全一致，有效消除段间跳色/亮度突变。
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
from typing import List, Optional, Sequence, Tuple


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".flv"}
DEFAULT_TRIM_FRAMES = 22
DEFAULT_AUDIO_RATE = 44100
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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

    # 1. 打包后优先从可执行文件同级目录 / bin / PyInstaller _MEIPASS 查找
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir / exe_name, exe_dir / "bin" / exe_name])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mp = Path(meipass)
            candidates.extend([mp / exe_name, mp / "bin" / exe_name])

    # 2. 脚本同级目录 / bin
    script_dir = Path(__file__).resolve().parent
    candidates.extend([script_dir / exe_name, script_dir / "bin" / exe_name])

    for cand in candidates:
        if cand.exists():
            return str(cand)

    # 3. 系统 PATH
    found = shutil.which(name)
    if found:
        return found

    # 4. 常见本地路径兜底
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
    """提取文件名中的数字部分从小到大排序，再用自然排序作消歧。"""
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
    """
    从视频中流式提取指定范围的帧（RGB24 原始字节），用于 AB 对照预览。
    仅解码需要的极少数帧，不落盘临时图片，内存开销极小。
    """
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
    blend_junctions: Optional[Sequence[bool]] = None,
) -> Tuple[str, bool]:
    n_videos = len(infos)
    if blend_junctions is None:
        junctions = [False] * max(0, n_videos - 1)
    else:
        if len(blend_junctions) != max(0, n_videos - 1):
            raise ValueError("blend_junctions 长度必须等于 视频数量 - 1")
        junctions = [bool(x) and trim_frames > 0 for x in blend_junctions]

    ref = infos[0]
    out_w, out_h = ref.width, ref.height
    out_fps = target_fps if target_fps is not None else ref.fps
    use_audio = any(info.has_audio for info in infos)

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
        need_head = idx > 0 and junctions[idx - 1]
        need_tail = idx < n_videos - 1 and junctions[idx]

        body_start = trim_frames if idx > 0 else 0
        body_end = (total_f - trim_frames) if need_tail else total_f

        if total_f > 0:
            if idx > 0 and trim_frames > 0 and total_f <= trim_frames:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以裁掉 {trim_frames} 帧。"
                )
            if need_head and need_tail and total_f < 2 * trim_frames:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以同时与前后段各融合 {trim_frames} 帧。"
                )
            if need_tail and not need_head and total_f < trim_frames:
                raise ValueError(
                    f"第 {idx + 1} 段视频《{infos[idx].path.name}》仅 {total_f} 帧，"
                    f"不足以截取尾部 {trim_frames} 帧做渐变。"
                )

        has_body = (total_f == 0) or (body_end > body_start)
        slices: List[Tuple[str, str]] = []

        if need_head:
            lbl = f"vh{idx}"
            head_labels[idx] = lbl
            slices.append((lbl, f"trim=start_frame=0:end_frame={trim_frames},setpts=PTS-STARTPTS"))

        if has_body:
            lbl = f"vb{idx}"
            body_labels[idx] = lbl
            if need_tail and total_f > 0:
                slices.append((lbl, f"trim=start_frame={body_start}:end_frame={body_end},setpts=PTS-STARTPTS"))
            elif body_start > 0:
                slices.append((lbl, f"trim=start_frame={body_start},setpts=PTS-STARTPTS"))
            else:
                slices.append((lbl, "setpts=PTS-STARTPTS"))

        if need_tail:
            lbl = f"vt{idx}"
            tail_labels[idx] = lbl
            tail_start = max(0, total_f - trim_frames)
            slices.append((lbl, f"trim=start_frame={tail_start},setpts=PTS-STARTPTS"))

        if len(slices) == 1:
            lbl, filt = slices[0]
            lines.append(f"[vn{idx}]{filt}[{lbl}];")
        else:
            split_outs = "".join(f"[vns{idx}_{k}]" for k in range(len(slices)))
            lines.append(f"[vn{idx}]split={len(slices)}{split_outs};")
            for k, (lbl, filt) in enumerate(slices):
                lines.append(f"[vns{idx}_{k}]{filt}[{lbl}];")

    # 3. 组装视频拼接链（逐接缝，硬切直接接主体，渐变则先 blend 重叠 N 帧再接）
    v_pieces: List[str] = []
    if body_labels[0] is not None:
        v_pieces.append(f"[{body_labels[0]}]")
    for j in range(n_videos - 1):
        if junctions[j]:
            k = trim_frames
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

    # 4. 组装音频链：硬切直接裁前 N/fps 秒后拼接；渐变则在接缝处做对应时长的 acrossfade
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
            overlap_sec = calc_trim_seconds(trim_frames, out_fps)
            out_lbl = "aout" if j == n_videos - 2 else f"aacc{j}"

            if junctions[j] and overlap_sec > 0.001:
                lines.append(
                    f"[{acc_lbl}][{next_lbl}]acrossfade=d={overlap_sec:.6f}:c1=tri:c2=tri[{out_lbl}]"
                    + (";" if j < n_videos - 2 else "")
                )
            else:
                if trim_frames > 0:
                    trim_sec = calc_trim_seconds(trim_frames, next_info.fps if next_info.fps == out_fps else out_fps)
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
    blend_junctions: Optional[Sequence[bool]] = None,
    log_cb=None,
) -> None:
    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)
        else:
            print(msg)

    filter_graph, use_audio = build_filter_graph(infos, trim_frames, target_fps, blend_junctions)
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


def parse_blend_arg(blend_arg: Optional[str], num_videos: int) -> List[bool]:
    junctions = [False] * max(0, num_videos - 1)
    if not blend_arg:
        return junctions
    raw = blend_arg.strip().lower()
    if raw in ("all", "true", "yes"):
        return [True] * len(junctions)
    if raw in ("none", "false", "no"):
        return junctions
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        # 支持 "1-2" 或 "1"（第1个接缝，即第1段与第2段之间）
        if "-" in token:
            a_str, _ = token.split("-", 1)
            idx = int(a_str.strip()) - 1
        else:
            idx = int(token) - 1
        if 0 <= idx < len(junctions):
            junctions[idx] = True
    return junctions


def print_summary(
    infos: List[VideoInfo],
    trim_frames: int,
    sort_label: str,
    encoder_mode: str,
    output_path: Path,
    blend_junctions: Sequence[bool],
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
            mode_str = "色度渐变融合" if blend_junctions[i - 1] else "硬切去重"
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
    parser.add_argument("--blend", type=str, default=None, help="开启色度渐变融合的接缝，如 all 或 1-2,3-4")
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

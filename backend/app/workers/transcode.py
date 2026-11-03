"""ffmpeg-based transcoding to multi-resolution MPEG-DASH.

Pure filesystem operations: takes a local input file, writes DASH output
(manifest + segments) to a local directory. S3 I/O lives in ``processing``.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DASH_MANIFEST_NAME = "manifest.mpd"


@dataclass(frozen=True)
class Rung:
    height: int
    width: int
    video_bitrate: str
    label: str


# Quality ladder, highest first. Rungs above the source resolution are skipped.
RESOLUTION_LADDER: tuple[Rung, ...] = (
    Rung(height=1080, width=1920, video_bitrate="5000k", label="1080p"),
    Rung(height=720, width=1280, video_bitrate="2800k", label="720p"),
    Rung(height=480, width=854, video_bitrate="1400k", label="480p"),
)

_AUDIO_BITRATE = "128k"


@dataclass(frozen=True)
class ProbeResult:
    height: int
    duration_seconds: float
    has_audio: bool


@dataclass(frozen=True)
class TranscodeResult:
    resolutions: list[str]
    duration_seconds: float
    manifest_name: str


class TranscodeError(Exception):
    """Raised when ffprobe/ffmpeg fails or the input is unusable."""


def probe_video(input_path: Path, ffprobe_path: str) -> ProbeResult:
    """Read source height, duration, and audio presence via ffprobe."""
    cmd = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(input_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscodeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise TranscodeError("no video stream found in input")

    height = int(video.get("height", 0))
    if height <= 0:
        raise TranscodeError("could not determine source video height")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(data.get("format", {}).get("duration", 0.0))

    return ProbeResult(height=height, duration_seconds=duration, has_audio=has_audio)


def select_rungs(source_height: int) -> list[Rung]:
    """Rungs at or below the source height; always keep at least the lowest."""
    eligible = [r for r in RESOLUTION_LADDER if r.height <= source_height]
    return eligible or [RESOLUTION_LADDER[-1]]


def build_dash_command(
    input_path: Path,
    output_dir: Path,
    rungs: list[Rung],
    *,
    has_audio: bool,
    ffmpeg_path: str,
) -> list[str]:
    """Assemble the ffmpeg command producing one DASH manifest with all rungs."""
    cmd: list[str] = [ffmpeg_path, "-y", "-i", str(input_path)]

    # Map the source video once per rung, plus audio if present.
    for _ in rungs:
        cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a:0"]

    cmd += ["-c:v", "libx264", "-preset", "veryfast"]
    for i, rung in enumerate(rungs):
        cmd += [
            f"-b:v:{i}",
            rung.video_bitrate,
            f"-s:v:{i}",
            f"{rung.width}x{rung.height}",
        ]

    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", _AUDIO_BITRATE]
        adaptation = "id=0,streams=v id=1,streams=a"
    else:
        adaptation = "id=0,streams=v"

    cmd += [
        "-use_timeline",
        "1",
        "-use_template",
        "1",
        "-adaptation_sets",
        adaptation,
        "-f",
        "dash",
        str(output_dir / DASH_MANIFEST_NAME),
    ]
    return cmd


def run_transcode(
    input_path: Path,
    output_dir: Path,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
) -> TranscodeResult:
    """Probe, transcode to DASH, and return the produced resolutions + duration."""
    probe = probe_video(input_path, ffprobe_path)
    rungs = select_rungs(probe.height)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = build_dash_command(
        input_path,
        output_dir,
        rungs,
        has_audio=probe.has_audio,
        ffmpeg_path=ffmpeg_path,
    )
    logger.info(
        "transcoding %s → %d rung(s): %s", input_path.name, len(rungs), [r.label for r in rungs]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscodeError(f"ffmpeg failed: {result.stderr.strip()[-2000:]}")

    return TranscodeResult(
        resolutions=[r.label for r in rungs],
        duration_seconds=probe.duration_seconds,
        manifest_name=DASH_MANIFEST_NAME,
    )

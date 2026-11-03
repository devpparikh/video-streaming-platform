"""ffmpeg-based thumbnail generation: a poster frame + a scrub-preview sprite.

Pure filesystem operations (mirrors ``transcode``): reads a local input file and
writes images + a WebVTT file into an output directory. S3 I/O lives in
``processing``. Two artifacts are produced:

  - poster.jpg      a single representative frame for the listing grid
  - sprite.jpg      a tiled grid of small frames (one every N seconds)
  - thumbnails.vtt  maps playback time ranges to sprite crops (#xywh=...)

The .vtt references the sprite by relative name, so both must be served from
the same path prefix.
"""

import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

POSTER_NAME = "poster.jpg"
SPRITE_NAME = "sprite.jpg"
VTT_NAME = "thumbnails.vtt"

# Poster: grabbed at 10% of duration so it isn't a black intro frame.
_POSTER_FRACTION = 0.1
_POSTER_WIDTH = 640

# Sprite: uniform cells tiled into a single sheet. The capture interval scales
# with video duration so short clips get fine scrubbing (~1s) while long videos
# stay coarse (~30s) — this keeps the sheet small without a tight thumb cap.
_THUMB_W = 160
_THUMB_H = 90
_SPRITE_COLS = 10

# Duration-tiered capture interval: (duration_upper_bound_seconds, interval_s).
# The first tier whose bound exceeds the duration wins; videos at or beyond the
# last bound use _INTERVAL_FALLBACK. Tune freely — finer = bigger sprite.
_INTERVAL_LADDER: tuple[tuple[float, int], ...] = (
    (2 * 60, 1),  # < 2 min  → every 1s  (very short: per-second scrubbing)
    (5 * 60, 2),  # < 5 min  → every 2s
    (10 * 60, 3),  # < 10 min → every 3s
    (30 * 60, 5),  # < 30 min → every 5s
    (60 * 60, 10),  # < 1 hr   → every 10s
    (2 * 60 * 60, 20),  # < 2 hr   → every 20s
)
_INTERVAL_FALLBACK = 30  # ≥ 2 hr → every 30s

# Backstop only: the ladder already bounds the count, but cap pathological
# durations so the sprite never exceeds browser image-decode limits
# (~16k px tall; 1500 thumbs ≈ 150 rows ≈ 13,500px).
_MAX_THUMBS = 1500


def _interval_for(duration: float) -> int:
    """Capture interval (seconds) for a video of the given duration."""
    for upper_bound, interval in _INTERVAL_LADDER:
        if duration < upper_bound:
            return interval
    return _INTERVAL_FALLBACK


class ThumbnailError(Exception):
    """Raised when ffmpeg fails to produce a thumbnail artifact."""


@dataclass(frozen=True)
class ThumbnailResult:
    poster_name: str
    sprite_name: str
    vtt_name: str


def _run(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ThumbnailError(f"ffmpeg {what} failed: {result.stderr.strip()[-1000:]}")


def _format_ts(seconds: float) -> str:
    """WebVTT timestamp: HH:MM:SS.mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def generate_poster(input_path: Path, out_dir: Path, *, duration: float, ffmpeg_path: str) -> str:
    ts = duration * _POSTER_FRACTION if duration > 0 else 0.0
    _run(
        [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{ts:.3f}",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_POSTER_WIDTH}:-2",
            str(out_dir / POSTER_NAME),
        ],
        "poster",
    )
    return POSTER_NAME


def _sprite_grid(duration: float) -> tuple[int, int, int, int]:
    """Return (interval, num_thumbs, cols, rows) for the sprite sheet.

    Interval is chosen from the duration ladder; the _MAX_THUMBS backstop only
    coarsens it further for pathologically long videos.
    """
    interval = _interval_for(duration)
    if duration <= 0:
        return interval, 1, 1, 1
    num = max(1, math.ceil(duration / interval))
    if num > _MAX_THUMBS:
        num = _MAX_THUMBS
        interval = math.ceil(duration / num)
    cols = min(_SPRITE_COLS, num)
    rows = math.ceil(num / cols)
    return interval, num, cols, rows


def generate_sprite_and_vtt(
    input_path: Path, out_dir: Path, *, duration: float, ffmpeg_path: str
) -> tuple[str, str]:
    interval, num, cols, rows = _sprite_grid(duration)

    vf = (
        f"fps=1/{interval},"
        f"scale={_THUMB_W}:{_THUMB_H}:force_original_aspect_ratio=decrease,"
        f"pad={_THUMB_W}:{_THUMB_H}:(ow-iw)/2:(oh-ih)/2,"
        f"tile={cols}x{rows}"
    )
    _run(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out_dir / SPRITE_NAME),
        ],
        "sprite",
    )

    cues: list[str] = []
    for i in range(num):
        start = i * interval
        if duration > 0 and start >= duration:
            break
        end = min((i + 1) * interval, duration) if duration > 0 else interval
        x, y = (i % cols) * _THUMB_W, (i // cols) * _THUMB_H
        cues.append(
            f"{_format_ts(start)} --> {_format_ts(end)}\n"
            f"{SPRITE_NAME}#xywh={x},{y},{_THUMB_W},{_THUMB_H}"
        )

    vtt = "WEBVTT\n\n" + "\n\n".join(cues) + "\n"
    (out_dir / VTT_NAME).write_text(vtt, encoding="utf-8")
    return SPRITE_NAME, VTT_NAME


def generate_thumbnails(
    input_path: Path, out_dir: Path, *, duration: float, ffmpeg_path: str
) -> ThumbnailResult:
    """Produce poster + sprite + VTT for a video. Output dir is created."""
    out_dir.mkdir(parents=True, exist_ok=True)
    poster = generate_poster(input_path, out_dir, duration=duration, ffmpeg_path=ffmpeg_path)
    sprite, vtt = generate_sprite_and_vtt(
        input_path, out_dir, duration=duration, ffmpeg_path=ffmpeg_path
    )
    logger.info(
        "generated thumbnails (%d-thumb sprite) for %s",
        _sprite_grid(duration)[1],
        input_path.name,
    )
    return ThumbnailResult(poster_name=poster, sprite_name=sprite, vtt_name=vtt)

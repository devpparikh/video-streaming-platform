"""Orchestrates one video processing job: S3 download → transcode → S3 upload.

Glue between the sync S3 client and the ffmpeg transcode module. Kept separate
so transcoding stays pure (local files only) and testable without S3.
"""

import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.s3 import open_s3_client_sync
from app.core.config import Settings
from app.workers.thumbnails import generate_thumbnails
from app.workers.transcode import run_transcode

logger = logging.getLogger(__name__)

# Content types so players/browsers receive correct MIME types from S3.
_CONTENT_TYPES: dict[str, str] = {
    ".mpd": "application/dash+xml",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".vtt": "text/vtt",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# Parallel S3 uploads for DASH segments — I/O-bound, so threads suffice.
_UPLOAD_CONCURRENCY = 8


@dataclass(frozen=True)
class ProcessingResult:
    resolutions: list[str]
    duration_seconds: float
    manifest_key: str


def raw_object_key(video_key: str) -> str:
    return f"uploads/{video_key}/raw"


def dash_prefix(video_key: str) -> str:
    return f"uploads/{video_key}/dash"


def thumbs_prefix(video_key: str) -> str:
    return f"uploads/{video_key}/thumbs"


def _content_type_for(filename: str) -> str:
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), _DEFAULT_CONTENT_TYPE)


def process_video_file(video_key: str, settings: Settings) -> ProcessingResult:
    """Download raw, transcode to DASH + thumbnails, and upload everything to S3."""
    s3: Any = open_s3_client_sync(settings)
    bucket = settings.s3_bucket

    with tempfile.TemporaryDirectory(prefix=f"vsp-{video_key}-") as tmp:
        tmp_dir = Path(tmp)
        raw_path = tmp_dir / "raw"
        dash_dir = tmp_dir / "dash"
        thumbs_dir = tmp_dir / "thumbs"

        logger.info("downloading raw for %s", video_key)
        s3.download_file(bucket, raw_object_key(video_key), str(raw_path))

        result = run_transcode(
            raw_path,
            dash_dir,
            ffmpeg_path=settings.ffmpeg_path,
            ffprobe_path=settings.ffprobe_path,
        )
        generate_thumbnails(
            raw_path,
            thumbs_dir,
            duration=result.duration_seconds,
            ffmpeg_path=settings.ffmpeg_path,
        )

        _upload_dir(s3, bucket, dash_prefix(video_key), dash_dir, "DASH")
        _upload_dir(s3, bucket, thumbs_prefix(video_key), thumbs_dir, "thumbnail")

    return ProcessingResult(
        resolutions=result.resolutions,
        duration_seconds=result.duration_seconds,
        manifest_key=f"{dash_prefix(video_key)}/{result.manifest_name}",
    )


def _upload_dir(s3: Any, bucket: str, prefix: str, local_dir: Path, label: str) -> None:
    """Upload every file in a local dir to ``prefix/`` in parallel."""
    files = [p for p in sorted(local_dir.iterdir()) if p.is_file()]

    def _upload(path: Path) -> None:
        s3.upload_file(
            str(path),
            bucket,
            f"{prefix}/{path.name}",
            ExtraArgs={"ContentType": _content_type_for(path.name)},
        )

    with ThreadPoolExecutor(max_workers=_UPLOAD_CONCURRENCY) as pool:
        # list() forces evaluation so any upload exception propagates here.
        list(pool.map(_upload, files))

    logger.info("uploaded %d %s file(s) under %s", len(files), label, prefix)

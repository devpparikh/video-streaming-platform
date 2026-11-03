"""Streams DASH artifacts (manifest + segments) from S3 to the player.

A backend passthrough: the manifest references segments by relative path, so
serving everything under one path-based route lets dash.js resolve segment URLs
without rewriting the manifest. Range requests are forwarded to S3 for seeking.
"""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends
from fastapi.responses import StreamingResponse

from app.common.deps import S3ClientDep, SettingsDep

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def dash_object_key(video_key: str, filename: str) -> str:
    """S3 key for a DASH artifact. Both args are validated upstream."""
    return f"uploads/{video_key}/dash/{filename}"


def asset_object_key(video_key: str, filename: str) -> str:
    """S3 key for a thumbnail asset (poster / sprite / vtt)."""
    return f"uploads/{video_key}/thumbs/{filename}"


def _cache_control(filename: str) -> str:
    # The manifest may change; everything else is content-named and immutable.
    if filename.endswith(".mpd"):
        return "no-cache"
    return "public, max-age=86400, immutable"


class VideoStreamService:
    def __init__(self, s3: S3ClientDep, settings: SettingsDep) -> None:
        self._s3 = s3
        self._settings = settings

    async def stream_dash_file(
        self, video_key: str, filename: str, range_header: str | None
    ) -> StreamingResponse:
        return await self._stream(dash_object_key(video_key, filename), filename, range_header)

    async def stream_asset(
        self, video_key: str, filename: str, range_header: str | None
    ) -> StreamingResponse:
        return await self._stream(asset_object_key(video_key, filename), filename, range_header)

    async def _stream(self, key: str, filename: str, range_header: str | None) -> StreamingResponse:
        params: dict[str, Any] = {"Bucket": self._settings.s3_bucket, "Key": key}
        if range_header:
            params["Range"] = range_header

        # ClientError (NoSuchKey → 404, InvalidRange → 416) handled globally.
        response = await self._s3.get_object(**params)

        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": _cache_control(filename),
            # These responses are cacheable but their CORS headers depend on the
            # request Origin. CORSMiddleware skips no-Origin requests entirely, so
            # without an explicit Vary the browser can cache a header-less copy and
            # later reuse it for a crossorigin request (e.g. vidstack thumbnail
            # sprites), which then fails CORS. Vary: Origin keys the cache correctly.
            "Vary": "Origin",
        }
        content_range = response.get("ContentRange")
        if content_range:
            headers["Content-Range"] = content_range
        content_length = response.get("ContentLength")
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        status_code = 206 if content_range else 200
        media_type = response.get("ContentType") or _DEFAULT_CONTENT_TYPE
        body = response["Body"]

        async def _iter_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in body.iter_chunks(_CHUNK_SIZE):
                    yield chunk
            finally:
                body.close()

        return StreamingResponse(
            _iter_body(),
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )


VideoStreamServiceDep = Annotated[VideoStreamService, Depends()]

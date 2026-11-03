import math
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.common.deps import S3ClientDep, S3PresignClientDep, SettingsDep
from app.core.identifiers import generate_video_key
from app.modules.uploads.schemas import (
    CompleteUploadRequest,
    CompleteUploadResponse,
    InitiateUploadRequest,
    InitiateUploadResponse,
    ListPartsResponse,
    SignPartsRequest,
    SignPartsResponse,
    UploadedPart,
)
from app.repositories.video_repository import (
    VideoRecord,
    VideoRepositoryDep,
    VideoStatus,
)

# S3 multipart constraints.
S3_MIN_PART_SIZE = 5 * 1024**2  # 5 MiB (all parts except the last)
S3_MAX_PARTS = 10_000
_MIB = 1024**2


def raw_object_key(video_key: str) -> str:
    """S3 key for the raw upload. Built only from the server-generated key."""
    return f"uploads/{video_key}/raw"


def compute_part_size(total_size: int, configured_part_size: int) -> int:
    """Smallest valid part size >= configured that keeps parts within S3 limits."""
    required = math.ceil(total_size / S3_MAX_PARTS)
    part_size = max(configured_part_size, required, S3_MIN_PART_SIZE)
    return math.ceil(part_size / _MIB) * _MIB  # round up to a whole MiB


def compute_part_count(total_size: int, part_size: int) -> int:
    return max(1, math.ceil(total_size / part_size))


class UploadService:
    """Orchestrates resumable S3 multipart uploads."""

    def __init__(
        self,
        s3_client: S3ClientDep,
        s3_presign_client: S3PresignClientDep,
        settings: SettingsDep,
        video_repo: VideoRepositoryDep,
    ) -> None:
        self._s3 = s3_client
        self._presign = s3_presign_client
        self._settings = settings
        self._video_repo = video_repo

    def _validate(self, request: InitiateUploadRequest) -> None:
        if request.content_type not in self._settings.allowed_content_types:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"Unsupported content type: {request.content_type}",
            )
        if request.size > self._settings.max_upload_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "File exceeds the maximum allowed upload size",
            )

    async def initiate_upload(self, request: InitiateUploadRequest) -> InitiateUploadResponse:
        self._validate(request)
        video_key = generate_video_key()
        part_size = compute_part_size(request.size, self._settings.multipart_part_size)
        part_count = compute_part_count(request.size, part_size)

        response = await self._s3.create_multipart_upload(
            Bucket=self._settings.s3_bucket,
            Key=raw_object_key(video_key),
            ContentType=request.content_type,
        )
        upload_id = response["UploadId"]
        now = datetime.now(UTC).isoformat()

        await self._video_repo.create(
            VideoRecord(
                video_key=video_key,
                upload_id=upload_id,
                title=request.title,
                category=request.category,
                original_filename=request.filename,
                size=request.size,
                content_type=request.content_type,
                status=VideoStatus.UPLOADING,
                created_at=now,
                updated_at=now,
            )
        )

        return InitiateUploadResponse(
            video_key=video_key,
            upload_id=upload_id,
            part_size=part_size,
            part_count=part_count,
        )

    async def sign_parts(self, video_key: str, request: SignPartsRequest) -> SignPartsResponse:
        """Presign PUT URLs for the given part numbers of an in-flight upload."""
        key = raw_object_key(video_key)
        expires_in = self._settings.presign_expiry_seconds

        urls: dict[int, str] = {}
        for part_number in request.part_numbers:
            urls[part_number] = await self._presign.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": self._settings.s3_bucket,
                    "Key": key,
                    "UploadId": request.upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_in,
            )

        return SignPartsResponse(urls=urls, expires_in=expires_in)

    def _derive_part_count(self, size: int) -> int:
        part_size = compute_part_size(size, self._settings.multipart_part_size)
        return compute_part_count(size, part_size)

    async def list_parts(self, video_key: str, upload_id: str) -> ListPartsResponse:
        """Return all parts S3 has already received for this multipart upload."""
        record = await self._video_repo.get(video_key)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")

        parts: list[UploadedPart] = []
        paginator_kwargs: dict = {
            "Bucket": self._settings.s3_bucket,
            "Key": raw_object_key(video_key),
            "UploadId": upload_id,
        }
        while True:
            response = await self._s3.list_parts(**paginator_kwargs)
            for p in response.get("Parts", []):
                parts.append(
                    UploadedPart(
                        part_number=p.get("PartNumber", 0),
                        etag=p.get("ETag", ""),
                        size=p.get("Size", 0),
                    )
                )
            if response.get("IsTruncated"):
                paginator_kwargs["PartNumberMarker"] = response["NextPartNumberMarker"]
            else:
                break

        return ListPartsResponse(parts=parts, part_count=self._derive_part_count(record.size))

    async def complete_upload(
        self, video_key: str, request: CompleteUploadRequest
    ) -> CompleteUploadResponse:
        """Validate all parts are present then finalize the S3 multipart upload."""
        record = await self._video_repo.get(video_key)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")

        expected = set(range(1, self._derive_part_count(record.size) + 1))
        submitted = {p.part_number for p in request.parts}
        if submitted != expected:
            missing = sorted(expected - submitted)
            extra = sorted(submitted - expected)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Parts mismatch — missing: {missing}, unexpected: {extra}",
            )

        await self._s3.complete_multipart_upload(
            Bucket=self._settings.s3_bucket,
            Key=raw_object_key(video_key),
            UploadId=request.upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": p.part_number, "ETag": p.etag}
                    for p in sorted(request.parts, key=lambda p: p.part_number)
                ]
            },
        )

        await self._video_repo.update_status(
            video_key,
            VideoStatus.QUEUED,
            updated_at=datetime.now(UTC).isoformat(),
        )

        from app.workers.tasks import process_video

        process_video.delay(video_key)

        return CompleteUploadResponse(video_key=video_key)


UploadServiceDep = Annotated[UploadService, Depends()]

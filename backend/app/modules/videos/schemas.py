from pydantic import BaseModel

from app.repositories.video_repository import VideoRecord


class VideoSummary(BaseModel):
    """Public listing shape — omits internal fields (upload_id, lease, etc.)."""

    video_key: str
    title: str
    category: str
    status: str
    created_at: str
    duration_seconds: float | None = None
    resolutions: list[str] = []
    # Poster image for the grid; set only once the video is ready.
    poster_path: str | None = None

    @classmethod
    def from_record(cls, record: VideoRecord, *, poster_path: str | None = None) -> "VideoSummary":
        return cls(
            video_key=record.video_key,
            title=record.title,
            category=record.category,
            status=record.status,
            created_at=record.created_at,
            duration_seconds=record.duration_seconds,
            resolutions=record.resolutions,
            poster_path=poster_path,
        )


class VideoDetail(BaseModel):
    """Full metadata for the detail page. Still omits internal upload fields."""

    video_key: str
    title: str
    category: str
    status: str
    created_at: str
    updated_at: str
    original_filename: str
    size: int
    duration_seconds: float | None = None
    resolutions: list[str] = []
    error_message: str | None = None
    # Streaming/preview paths; set only once the video is ready.
    manifest_path: str | None = None
    poster_path: str | None = None
    thumbnails_vtt_path: str | None = None

    @classmethod
    def from_record(
        cls,
        record: VideoRecord,
        *,
        manifest_path: str | None,
        poster_path: str | None = None,
        thumbnails_vtt_path: str | None = None,
    ) -> "VideoDetail":
        return cls(
            video_key=record.video_key,
            title=record.title,
            category=record.category,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            original_filename=record.original_filename,
            size=record.size,
            duration_seconds=record.duration_seconds,
            resolutions=record.resolutions,
            error_message=record.error_message,
            manifest_path=manifest_path,
            poster_path=poster_path,
            thumbnails_vtt_path=thumbnails_vtt_path,
        )


class VideoListData(BaseModel):
    items: list[VideoSummary]
    next_cursor: str | None = None


class CategoryListData(BaseModel):
    categories: list[str]


class DeleteVideoData(BaseModel):
    video_key: str
    deleted: bool = True


class VideoVisibilityData(BaseModel):
    video_key: str
    visibility: str

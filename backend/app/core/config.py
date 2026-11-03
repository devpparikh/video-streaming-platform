from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "StreamForge API"
    environment: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api"
    backend_cors_origins: str = ""
    data_dir: Path = Path("../data")
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # AWS / S3. The endpoint URLs below default to the local emulator (:4566).
    #
    # PRODUCTION (real AWS): set both endpoint URLs to "" (empty) so boto3 uses
    # the real regional AWS endpoints, set S3_USE_PATH_STYLE="false" (real S3
    # prefers virtual-hosted style), and provide real credentials + region +
    # bucket. Nothing else changes — the app code is endpoint-agnostic.
    #
    # aws_endpoint_url is the host the SERVER reaches S3 on (e.g.
    # http://aws:4566 inside compose); aws_public_endpoint_url is the
    # browser-reachable host that presigned URLs are signed against. On real
    # AWS leave both empty and they collapse to the real endpoint.
    aws_endpoint_url: str = "http://localhost:4566"
    aws_public_endpoint_url: str = "http://localhost:4566"
    aws_default_region: str = "us-east-1"
    # Secrets — no defaults. Missing values fail fast at startup (see get_settings).
    aws_access_key_id: str
    aws_secret_access_key: str
    s3_bucket: str = "videos"
    s3_use_path_style: bool = True

    # DynamoDB. Same production note as S3: set to "" for real AWS.
    aws_dynamodb_endpoint_url: str = "http://localhost:4566"
    dynamodb_video_table: str = "video-metadata"

    # Upload limits / multipart tuning.
    max_upload_bytes: int = 5 * 1024**3  # 5 GiB
    multipart_part_size: int = 16 * 1024**2  # 16 MiB
    allowed_upload_content_types: str = "video/mp4,video/quicktime,video/x-matroska,video/webm"
    # Predefined category catalog (JSON array of slugs). Single source of truth
    # for upload validation and the categories endpoint. Override via env.
    categories_file: Path = Path("app/data/categories.json")
    presign_expiry_seconds: int = 900  # 15 min — presigned URL lifetime
    worker_lease_seconds: int = 600  # 10 min — job lease duration
    worker_lease_renew_interval: int = 120  # 2 min — how often worker renews lease

    # Transcoding (ffmpeg). Paths default to binaries on PATH.
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]

    @property
    def allowed_content_types(self) -> set[str]:
        return {
            item.strip() for item in self.allowed_upload_content_types.split(",") if item.strip()
        }


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # values come from env/.env
    except ValidationError as exc:
        missing = sorted(
            str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"
        )
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Set them in the environment or a .env file (see .env.example)."
            ) from exc
        raise

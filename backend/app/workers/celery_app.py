from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "video_streaming_platform",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # Import the task module so the worker registers process_video. Without this
    # the worker boots with an empty [tasks] list and never consumes the queue.
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

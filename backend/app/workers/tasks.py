import logging
import threading
from datetime import UTC, datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.repositories.video_repository import VideoStatus
from app.repositories.video_repository_sync import SyncVideoRepository
from app.workers.celery_app import celery_app
from app.workers.processing import process_video_file

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lease_expiry(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


@celery_app.task(
    name="process_video",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=3600,
)
def process_video(self, video_key: str) -> None:
    settings = get_settings()
    repo = SyncVideoRepository(settings)

    claimed = repo.claim_for_processing(
        video_key,
        locked_until=_lease_expiry(settings.worker_lease_seconds),
        updated_at=_now(),
    )
    if not claimed:
        logger.info("video %s already claimed — re-queuing after lease window", video_key)
        raise self.retry(
            countdown=settings.worker_lease_seconds + 30,
            max_retries=10,
        )

    stop_event = threading.Event()
    renewer = threading.Thread(
        target=_run_lease_renewer,
        args=(
            repo,
            video_key,
            settings.worker_lease_seconds,
            settings.worker_lease_renew_interval,
            stop_event,
        ),
        daemon=True,
    )
    renewer.start()

    try:
        result = process_video_file(video_key, settings)
        stop_event.set()
        renewer.join()
        repo.mark_ready(
            video_key,
            updated_at=_now(),
            resolutions=result.resolutions,
            duration_seconds=result.duration_seconds,
        )

    except SoftTimeLimitExceeded:
        stop_event.set()
        logger.warning("video %s hit soft time limit", video_key)
        repo.update_status(
            video_key,
            VideoStatus.FAILED,
            updated_at=_now(),
            error_message="processing timed out",
        )

    except Exception as exc:
        stop_event.set()
        renewer.join()
        if self.request.retries >= self.max_retries:
            logger.error("video %s failed after %d retries: %s", video_key, self.max_retries, exc)
            repo.update_status(
                video_key,
                VideoStatus.FAILED,
                updated_at=_now(),
                error_message=str(exc),
            )
            return
        repo.release_to_queued(video_key, updated_at=_now())
        raise self.retry(exc=exc) from exc

    finally:
        stop_event.set()


def _run_lease_renewer(
    repo: SyncVideoRepository,
    video_key: str,
    lease_seconds: int,
    renew_interval: int,
    stop: threading.Event,
) -> None:
    while not stop.wait(timeout=renew_interval):
        try:
            repo.renew_lease(
                video_key,
                locked_until=_lease_expiry(lease_seconds),
                updated_at=_now(),
            )
            logger.debug("renewed lease for video %s", video_key)
        except Exception:
            logger.warning("failed to renew lease for video %s", video_key, exc_info=True)

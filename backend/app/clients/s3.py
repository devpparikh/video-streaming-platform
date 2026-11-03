"""Async S3 clients (aioboto3).

aioboto3 clients are async context managers bound to the running event loop, so
instead of rebuilding one per request we open them once at app startup (see the
lifespan in ``app.main``) and keep them on ``app.state`` for the process
lifetime.

Two clients exist because the server and the browser reach S3 on different
hosts: the server uses the internal endpoint; presigned URLs must be signed
against the public, browser-reachable endpoint. On real AWS both collapse to the
real regional endpoint.
"""

from contextlib import AsyncExitStack
from functools import lru_cache
from typing import Any

import aioboto3
from aiobotocore.client import AioBaseClient
from aiobotocore.config import AioConfig

from app.core.config import Settings, get_settings


@lru_cache
def get_session() -> aioboto3.Session:
    """Shared aioboto3 session (cheap, reusable across the process)."""
    return aioboto3.Session()


def _client(session: aioboto3.Session, settings: Settings, endpoint_url: str):
    addressing_style = "path" if settings.s3_use_path_style else "auto"
    return session.client(
        "s3",
        # Empty endpoint => real AWS: aiobotocore resolves the regional endpoint.
        endpoint_url=endpoint_url or None,
        region_name=settings.aws_default_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


async def open_s3_clients(
    stack: AsyncExitStack,
) -> tuple[AioBaseClient, AioBaseClient]:
    """Open long-lived (server, presign) S3 clients on the given exit stack."""
    session = get_session()
    settings = get_settings()
    server = await stack.enter_async_context(_client(session, settings, settings.aws_endpoint_url))
    presign = await stack.enter_async_context(
        _client(
            session,
            settings,
            settings.aws_public_endpoint_url or settings.aws_endpoint_url,
        )
    )
    return server, presign


def open_s3_client_sync(settings: Settings) -> Any:
    """Synchronous S3 client for Celery workers (boto3, internal endpoint)."""
    import boto3
    from botocore.config import Config

    addressing_style = "path" if settings.s3_use_path_style else "auto"
    return boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint_url or None,
        region_name=settings.aws_default_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )

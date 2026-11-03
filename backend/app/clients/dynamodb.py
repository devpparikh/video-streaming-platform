"""Async DynamoDB client (aioboto3).

Opened once at app startup via lifespan (same pattern as S3).

PRODUCTION: set AWS_DYNAMODB_ENDPOINT_URL="" so aiobotocore resolves the real
regional DynamoDB endpoint. Nothing else changes.
"""

from contextlib import AsyncExitStack
from typing import Any

import aioboto3
from aiobotocore.client import AioBaseClient

from app.clients.s3 import get_session
from app.core.config import Settings, get_settings


def _client(session: aioboto3.Session, settings: Settings):
    return session.client(
        "dynamodb",
        endpoint_url=settings.aws_dynamodb_endpoint_url or None,
        region_name=settings.aws_default_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


async def open_dynamodb_client(stack: AsyncExitStack) -> AioBaseClient:
    """Open a long-lived DynamoDB client on the given exit stack."""
    session = get_session()
    settings = get_settings()
    return await stack.enter_async_context(_client(session, settings))


def open_dynamodb_client_sync(settings: Settings) -> Any:
    """Synchronous DynamoDB client for Celery workers (boto3)."""
    import boto3

    return boto3.client(
        "dynamodb",
        endpoint_url=settings.aws_dynamodb_endpoint_url or None,
        region_name=settings.aws_default_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )

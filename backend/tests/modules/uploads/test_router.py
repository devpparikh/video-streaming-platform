from collections.abc import AsyncIterator

import aioboto3
import pytest
from aiobotocore.client import AioBaseClient
from aiomoto import mock_aws
from httpx import AsyncClient

from app.common.deps import get_s3_client, get_s3_presign_client
from app.core.identifiers import is_valid_video_key
from app.main import app
from app.repositories.video_repository import _get_dynamo_client

_DYNAMO_KWARGS = dict(
    region_name="us-east-1",
    aws_access_key_id="test",
    aws_secret_access_key="test",
)


@pytest.fixture
async def aws_override() -> AsyncIterator[AioBaseClient]:
    """In-process (aiomoto) S3 + DynamoDB clients, injected via dependency overrides."""
    async with mock_aws():
        session = aioboto3.Session()
        async with (
            session.client("s3", **_DYNAMO_KWARGS) as s3,
            session.client("dynamodb", **_DYNAMO_KWARGS) as dynamo,
        ):
            await s3.create_bucket(Bucket="videos")
            await dynamo.create_table(
                TableName="video-metadata",
                KeySchema=[{"AttributeName": "video_key", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "video_key", "AttributeType": "S"},
                    {"AttributeName": "category", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "category-created_at-index",
                        "KeySchema": [
                            {"AttributeName": "category", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            app.dependency_overrides[get_s3_client] = lambda: s3
            app.dependency_overrides[get_s3_presign_client] = lambda: s3
            app.dependency_overrides[_get_dynamo_client] = lambda: dynamo
            try:
                yield s3
            finally:
                app.dependency_overrides.pop(get_s3_client, None)
                app.dependency_overrides.pop(get_s3_presign_client, None)
                app.dependency_overrides.pop(_get_dynamo_client, None)


def _payload(**overrides) -> dict:
    payload = {
        "filename": "clip.mp4",
        "content_type": "video/mp4",
        "size": 100 * 1024 * 1024,
        "title": "My Clip",
        "category": "movies",
    }
    payload.update(overrides)
    return payload


async def test_create_upload_starts_multipart(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    resp = await client.post("/api/uploads", json=_payload())

    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert is_valid_video_key(data["video_key"])
    assert data["upload_id"]
    assert data["part_size"] >= 5 * 1024 * 1024
    assert data["part_count"] >= 1


async def test_create_upload_rejects_unsupported_type(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    resp = await client.post("/api/uploads", json=_payload(content_type="application/zip"))
    assert resp.status_code == 415
    assert resp.json()["success"] is False


async def test_create_upload_rejects_too_large(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    resp = await client.post("/api/uploads", json=_payload(size=10 * 1024**4))
    assert resp.status_code == 413
    assert resp.json()["success"] is False


async def test_create_upload_validates_body(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    resp = await client.post("/api/uploads", json=_payload(size=0))
    assert resp.status_code == 422
    assert resp.json()["success"] is False


async def _initiate(client: AsyncClient) -> dict:  # noqa: RUF029
    resp = await client.post("/api/uploads", json=_payload())
    assert resp.status_code == 201
    return resp.json()["data"]


async def test_sign_parts_returns_presigned_urls(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    upload = await _initiate(client)
    resp = await client.post(
        f"/api/uploads/{upload['video_key']}/parts:sign",
        json={"upload_id": upload["upload_id"], "part_numbers": [1, 2, 3]},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data["urls"].keys()) == {"1", "2", "3"}
    assert all(url.startswith("http") for url in data["urls"].values())
    assert data["expires_in"] == 900


async def test_sign_parts_rejects_invalid_video_key(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    resp = await client.post(
        "/api/uploads/not-a-valid-key/parts:sign",
        json={"upload_id": "x", "part_numbers": [1]},
    )
    assert resp.status_code == 422


async def test_sign_parts_rejects_empty_batch(
    aws_override: AioBaseClient, client: AsyncClient
) -> None:
    upload = await _initiate(client)
    resp = await client.post(
        f"/api/uploads/{upload['video_key']}/parts:sign",
        json={"upload_id": upload["upload_id"], "part_numbers": []},
    )
    assert resp.status_code == 422

"""Video metadata repository (DynamoDB).

Table: video-metadata
  PK: video_key  (string, uuid4 hex)
  GSI: category-created_at-index  (category → created_at sort)

All writes are atomic item-level operations. Concurrent updates use
ConditionExpression to prevent clobbering.
"""

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from botocore.exceptions import ClientError
from fastapi import Depends, Request
from pydantic import BaseModel

from app.common.deps import SettingsDep

CATEGORY_INDEX = "category-created_at-index"
VISIBILITY_INDEX = "visibility-created_at-index"
# visibility partitions the catalog so the all-videos feed can be Queried (and
# sorted by created_at) — a Scan can't sort. Doubles as a soft-delete flag.
VISIBILITY_ACTIVE = "active"
VISIBILITY_INACTIVE = "inactive"
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100

if TYPE_CHECKING:
    from types_aiobotocore_dynamodb import DynamoDBClient
else:
    from aiobotocore.client import AioBaseClient as DynamoDBClient


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VideoStatus:
    UPLOADING = "uploading"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class VideoRecord(BaseModel):
    video_key: str
    upload_id: str
    title: str
    category: str
    original_filename: str
    size: int
    content_type: str
    status: str
    created_at: str
    updated_at: str
    duration_seconds: float | None = None
    resolutions: list[str] = []
    error_message: str | None = None
    locked_until: str | None = None


@dataclass(frozen=True)
class VideoPage:
    """One page of list results. ``next_cursor`` is None when fully consumed."""

    items: list[VideoRecord]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def _get_dynamo_client(request: Request) -> "DynamoDBClient":
    return request.app.state.dynamodb_client


DynamoDBClientDep = Annotated["DynamoDBClient", Depends(_get_dynamo_client)]


class VideoRepository:
    def __init__(
        self,
        dynamo: DynamoDBClientDep,
        settings: SettingsDep,
    ) -> None:
        self._dynamo = dynamo
        self._table = settings.dynamodb_video_table

    async def create(self, record: VideoRecord) -> None:
        """Write a new video record. Raises if video_key already exists."""
        item: dict[str, Any] = {
            "video_key": {"S": record.video_key},
            "upload_id": {"S": record.upload_id},
            "title": {"S": record.title},
            "category": {"S": record.category},
            "original_filename": {"S": record.original_filename},
            "size": {"N": str(record.size)},
            "content_type": {"S": record.content_type},
            "status": {"S": record.status},
            "created_at": {"S": record.created_at},
            "updated_at": {"S": record.updated_at},
            "resolutions": {"L": []},
            "visibility": {"S": VISIBILITY_ACTIVE},  # powers the all-videos GSI
        }
        if record.duration_seconds is not None:
            item["duration_seconds"] = {"N": str(record.duration_seconds)}
        if record.error_message is not None:
            item["error_message"] = {"S": record.error_message}

        await self._dynamo.put_item(
            TableName=self._table,
            Item=item,
            ConditionExpression="attribute_not_exists(video_key)",
        )

    async def get(self, video_key: str) -> VideoRecord | None:
        """Return the record for video_key, or None if not found."""
        response = await self._dynamo.get_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
        )
        item = response.get("Item")
        if item is None:
            return None
        return _deserialize(item)

    async def delete(self, video_key: str) -> None:
        """Hard-delete the record. Idempotent — no-op if it doesn't exist."""
        await self._dynamo.delete_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
        )

    async def set_visibility(self, video_key: str, visibility: str, updated_at: str) -> bool:
        """Soft-delete toggle: set active/inactive. False if the video is gone."""
        try:
            await self._dynamo.update_item(
                TableName=self._table,
                Key={"video_key": {"S": video_key}},
                UpdateExpression="SET visibility = :vis, updated_at = :ua",
                ConditionExpression="attribute_exists(video_key)",
                ExpressionAttributeValues={
                    ":vis": {"S": visibility},
                    ":ua": {"S": updated_at},
                },
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    async def list_videos(
        self,
        *,
        category: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        visibility: str = VISIBILITY_ACTIVE,
    ) -> "VideoPage":
        """List videos with optional category/status filters.

        Both paths Query a GSI sorted by created_at descending (newest first):
        by category, or by the ``visibility`` partition for the full catalog.
        Only videos matching ``visibility`` (default active) are returned.
        """
        start_key = _decode_cursor(cursor)
        if category:
            response = await self._query_by_category(category, status, visibility, limit, start_key)
        else:
            response = await self._query_by_visibility(visibility, status, limit, start_key)

        items = [_deserialize(i) for i in response.get("Items", [])]
        return VideoPage(
            items=items,
            next_cursor=_encode_cursor(response.get("LastEvaluatedKey")),
        )

    async def _query_by_category(
        self,
        category: str,
        status: str | None,
        visibility: str,
        limit: int,
        start_key: dict | None,
    ):
        # visibility isn't a key on this index, so filter on it to keep hidden
        # videos out of category browsing too.
        filters = ["visibility = :vis"]
        names: dict[str, str] = {}
        values: dict[str, Any] = {
            ":cat": {"S": category},
            ":vis": {"S": visibility},
        }
        if status:
            filters.append("#s = :st")
            names["#s"] = "status"
            values[":st"] = {"S": status}

        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "IndexName": CATEGORY_INDEX,
            "KeyConditionExpression": "category = :cat",
            "FilterExpression": " AND ".join(filters),
            "ExpressionAttributeValues": values,
            "ScanIndexForward": False,  # newest first
            "Limit": limit,
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        return await self._dynamo.query(**kwargs)

    async def _query_by_visibility(
        self, visibility: str, status: str | None, limit: int, start_key: dict | None
    ):
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "IndexName": VISIBILITY_INDEX,
            "KeyConditionExpression": "visibility = :vis",
            "ExpressionAttributeValues": {":vis": {"S": visibility}},
            "ScanIndexForward": False,  # newest first
            "Limit": limit,
        }
        if status:
            kwargs["FilterExpression"] = "#s = :st"
            kwargs["ExpressionAttributeNames"] = {"#s": "status"}
            kwargs["ExpressionAttributeValues"][":st"] = {"S": status}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        return await self._dynamo.query(**kwargs)

    async def claim_for_processing(
        self, video_key: str, locked_until: str, updated_at: str
    ) -> bool:
        """Atomically claim a video for processing using a lease.

        Succeeds if status is QUEUED, or status is PROCESSING with an expired lease.
        Returns False if another worker already holds a valid lease.
        """
        try:
            await self._dynamo.update_item(
                TableName=self._table,
                Key={"video_key": {"S": video_key}},
                UpdateExpression="SET #s = :processing, locked_until = :lu, updated_at = :ua",
                ConditionExpression=("#s = :queued OR (#s = :processing AND locked_until < :now)"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":processing": {"S": VideoStatus.PROCESSING},
                    ":queued": {"S": VideoStatus.QUEUED},
                    ":lu": {"S": locked_until},
                    ":now": {"S": updated_at},
                    ":ua": {"S": updated_at},
                },
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    async def renew_lease(self, video_key: str, locked_until: str, updated_at: str) -> None:
        """Extend the lease for an in-progress job. Called periodically by the worker."""
        await self._dynamo.update_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
            UpdateExpression="SET locked_until = :lu, updated_at = :ua",
            ConditionExpression="#s = :processing",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":lu": {"S": locked_until},
                ":ua": {"S": updated_at},
                ":processing": {"S": VideoStatus.PROCESSING},
            },
        )

    async def update_status(
        self,
        video_key: str,
        status: str,
        updated_at: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update the status field (and optional error) for an existing record."""
        expr = "SET #s = :s, updated_at = :ua REMOVE locked_until"
        names: dict[str, str] = {"#s": "status"}
        values: dict[str, Any] = {
            ":s": {"S": status},
            ":ua": {"S": updated_at},
        }
        if error_message is not None:
            expr += ", error_message = :em"
            values[":em"] = {"S": error_message}

        await self._dynamo.update_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(video_key)",
        )


VideoRepositoryDep = Annotated[VideoRepository, Depends()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_cursor(last_key: dict | None) -> str | None:
    """Encode a DynamoDB LastEvaluatedKey as an opaque base64 cursor."""
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_cursor(cursor: str | None) -> dict | None:
    """Decode a pagination cursor back into an ExclusiveStartKey."""
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _deserialize(item: dict[str, Any]) -> VideoRecord:
    def s(key: str) -> str:
        return item[key]["S"]

    def n(key: str) -> str:
        return item[key]["N"]

    return VideoRecord(
        video_key=s("video_key"),
        upload_id=s("upload_id"),
        title=s("title"),
        category=s("category"),
        original_filename=s("original_filename"),
        size=int(n("size")),
        content_type=s("content_type"),
        status=s("status"),
        created_at=s("created_at"),
        updated_at=s("updated_at"),
        duration_seconds=float(n("duration_seconds")) if "duration_seconds" in item else None,
        resolutions=[r["S"] for r in item.get("resolutions", {}).get("L", [])],
        error_message=item["error_message"]["S"] if "error_message" in item else None,
        locked_until=item["locked_until"]["S"] if "locked_until" in item else None,
    )

"""Synchronous VideoRepository for Celery workers (boto3)."""

from typing import Any

from botocore.exceptions import ClientError

from app.clients.dynamodb import open_dynamodb_client_sync
from app.core.config import Settings
from app.repositories.video_repository import VideoStatus


class SyncVideoRepository:
    def __init__(self, settings: Settings) -> None:
        self._client: Any = open_dynamodb_client_sync(settings)
        self._table = settings.dynamodb_video_table

    def claim_for_processing(self, video_key: str, locked_until: str, updated_at: str) -> bool:
        try:
            self._client.update_item(
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

    def renew_lease(self, video_key: str, locked_until: str, updated_at: str) -> None:
        self._client.update_item(
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

    def release_to_queued(self, video_key: str, updated_at: str) -> None:
        """Reset a job back to QUEUED and drop its lease, so a retry can re-claim it."""
        self._client.update_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
            UpdateExpression="SET #s = :queued, updated_at = :ua REMOVE locked_until",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": VideoStatus.QUEUED},
                ":ua": {"S": updated_at},
            },
        )

    def mark_ready(
        self,
        video_key: str,
        updated_at: str,
        *,
        resolutions: list[str],
        duration_seconds: float,
    ) -> None:
        """Transition a job to READY with its produced resolutions and duration."""
        self._client.update_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
            UpdateExpression=(
                "SET #s = :s, updated_at = :ua, resolutions = :res, "
                "duration_seconds = :dur REMOVE locked_until, error_message"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": VideoStatus.READY},
                ":ua": {"S": updated_at},
                ":res": {"L": [{"S": r} for r in resolutions]},
                ":dur": {"N": str(duration_seconds)},
            },
            ConditionExpression="attribute_exists(video_key)",
        )

    def update_status(
        self,
        video_key: str,
        status: str,
        updated_at: str,
        *,
        error_message: str | None = None,
    ) -> None:
        expr = "SET #s = :s, updated_at = :ua REMOVE locked_until"
        names: dict[str, str] = {"#s": "status"}
        values: dict[str, Any] = {
            ":s": {"S": status},
            ":ua": {"S": updated_at},
        }
        if error_message is not None:
            expr += ", error_message = :em"
            values[":em"] = {"S": error_message}

        self._client.update_item(
            TableName=self._table,
            Key={"video_key": {"S": video_key}},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(video_key)",
        )

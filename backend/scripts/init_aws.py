"""Provision the S3 bucket and DynamoDB table on the local AWS emulator.

Mirrors infra/terraform (bucket + CORS + versioning, table + both GSIs) so the
local stack comes up with no Terraform and no emulator account. Idempotent:
safe to run on every `docker compose up`.

Run from the backend/ directory (compose runs it as the `aws-init` service):
    python scripts/init_aws.py
"""

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Make the backend root importable regardless of the caller's CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402

settings = get_settings()
cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")


def client(service: str, endpoint: str):
    return boto3.client(
        service,
        endpoint_url=endpoint or None,
        region_name=settings.aws_default_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def ensure_bucket() -> None:
    s3 = client("s3", settings.aws_endpoint_url)
    bucket = settings.s3_bucket
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket {bucket}: exists")
    except ClientError:
        s3.create_bucket(Bucket=bucket)
        print(f"bucket {bucket}: created")
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    # The browser PUTs parts straight to S3 and must read each part's ETag.
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "HEAD", "PUT"],
                    "AllowedOrigins": cors_origins,
                    "ExposeHeaders": ["ETag", "Content-Range", "Accept-Ranges", "Content-Length"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    )


def ensure_table() -> None:
    ddb = client("dynamodb", settings.aws_dynamodb_endpoint_url)
    table = settings.dynamodb_video_table
    if table in ddb.list_tables()["TableNames"]:
        print(f"table {table}: exists")
        return

    def gsi(name: str, hash_key: str) -> dict:
        return {
            "IndexName": name,
            "KeySchema": [
                {"AttributeName": hash_key, "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }

    ddb.create_table(
        TableName=table,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "video_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("video_key", "category", "created_at", "visibility")
        ],
        GlobalSecondaryIndexes=[
            gsi("category-created_at-index", "category"),
            gsi("visibility-created_at-index", "visibility"),
        ],
    )
    print(f"table {table}: created")


if __name__ == "__main__":
    ensure_bucket()
    ensure_table()

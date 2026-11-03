# Terraform — AWS resources

Provisions the S3 bucket and DynamoDB table StreamForge needs on **real AWS**.

For local development you don't need this: `docker compose up` runs a local
emulator and the `aws-init` service creates the same resources with
[`backend/scripts/init_aws.py`](../../backend/scripts/init_aws.py).

## Prerequisites

1. Terraform >= 1.6.
2. AWS credentials configured (`aws configure`, environment variables, or SSO).

## Usage

Run from `infra/terraform/`:

```bash
cp terraform.tfvars.example terraform.tfvars   # optional: names, region, CORS origins
terraform init
terraform plan
terraform apply
```

Set `cors_allowed_origins` to your frontend's origin. Then point the backend at
AWS as described in the [root README](../../README.md#deploying-to-real-aws).

## What it creates

| Resource | Purpose |
|----------|---------|
| `aws_s3_bucket.videos` | Raw uploads + transcoded DASH output |
| `aws_s3_bucket_versioning` | Guards against accidental overwrites |
| `aws_s3_bucket_cors_configuration` | Lets the browser PUT upload parts and read ETags |
| `aws_s3_bucket_public_access_block` | Keeps the bucket private (segments are served through the API) |
| `aws_dynamodb_table.video_metadata` | Source-of-truth metadata per video |

### DynamoDB key design

**`video-metadata`**:

| Key | Attribute | Type | Why |
|-----|-----------|------|-----|
| PK | `video_key` | S | uuid4 hex; single-item `GET /videos/{key}` |
| GSI `category-created_at-index` PK | `category` | S | browse by category |
| GSI `visibility-created_at-index` PK | `visibility` | S | the full feed, excluding hidden videos |
| both GSIs, SK | `created_at` | S (ISO 8601) | newest first |

> No base-table sort key on purpose — `video_key` is already unique, so a
> range key would be artificial. Sorted browsing lives on the GSIs.

The table uses `PAY_PER_REQUEST` (on-demand) billing, so there's no capacity
planning. Non-key attributes (title, status, duration, resolutions, error, …)
are schemaless and not declared in Terraform.

## Outputs

- `videos_bucket_name` — bucket name for app config.
- `video_metadata_table` — DynamoDB table name.

## Notes

- State is local (`terraform.tfstate`) and git-ignored. Use a remote backend
  (S3 + DynamoDB lock table) if more than one person applies.
- `provider.tf` sets `s3_use_path_style = true`, which real S3 still accepts.
  The same files also work against a local emulator with
  [`tflocal`](https://github.com/localstack/terraform-local).

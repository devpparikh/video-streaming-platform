# StreamForge — Backend Documentation

A FastAPI backend for resumable video uploads, multi-resolution transcoding, and MPEG-DASH streaming, built for secure, scalable, low-latency delivery. Uploads are S3-based multipart resumable transfers. Transcoding is asynchronous via Celery. Videos are streamed as DASH manifests with H.264 video and AAC audio at adaptive bitrates.

## Overview

The backend serves these core responsibilities:

1. **Resumable uploads** — S3 multipart presigned URLs (4-step handshake), resumable via ETag matching.
2. **Transcoding pipeline** — Celery workers claim jobs via DynamoDB leases, download raw video, ffmpeg → MPEG-DASH (1080p/720p/480p, skipping resolution upscales), upload segments + manifest to S3.
3. **Catalog & streaming** — DynamoDB records metadata; soft-delete (hide/restore) and hard-delete support. DASH streaming endpoint with range-request support for seeking.
4. **No authentication, no users** — simpler design for MVP; all videos are public once ready.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend                           │
│  (async HTTP, long-lived S3/DynamoDB clients on app state)       │
├──────────────┬──────────────────────────┬───────────┬────────────┤
│  Health      │  Uploads (multipart)     │  Videos   │  Streaming │
│  /health     │  /uploads (init, sign,   │  (list,   │  /dash/    │
│              │  list, complete)         │  detail)  │  (range)   │
└──────────────┴──────────────────────────┴───────────┴────────────┘
       │               │                        │            │
       └───────────────┴────────────────────────┴────────────┘
                      │
                      ▼
        ┌─────────────────────────────┐
        │   DynamoDB (video-metadata) │ ◄── all metadata: status, resolutions,
        │   + 2 GSIs (category, all)  │     lease, duration
        └─────────────────────────────┘
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           ▼
    ┌────────────────┐      ┌────────────────┐
    │  S3 (raw)      │      │  S3 (dash/)    │
    │  uploads/key/  │      │  manifest.mpd  │
    │  raw           │      │  *.m4s         │
    └────────────────┘      └────────────────┘

       Redis Broker + Result Backend
              │
              ▼
    ┌──────────────────────────────────┐
    │  Celery Worker                   │
    │  (lease-based claim, ffmpeg)     │
    │  • Claim job (lease = 10 min)    │
    │  • Renew lease every 2 min       │
    │  • Download raw, transcode       │
    │  • Upload DASH to S3 (parallel)  │
    │  • Mark ready / release on fail  │
    └──────────────────────────────────┘
```

## Project Structure

```
backend/
├── app/
│   ├── main.py                           # FastAPI app creation, lifespan, router registration
│   ├── core/
│   │   ├── config.py                     # Settings: AWS, upload limits, paths, timings
│   │   ├── categories.py                 # Category catalog from JSON (immutable)
│   │   ├── identifiers.py                # video_key and filename validation patterns
│   │   └── logging.py                    # Logger configuration
│   │
│   ├── common/
│   │   ├── schemas.py                    # ApiResponse[T] envelope (success, data, error)
│   │   ├── errors.py                     # Global exception handlers (S3 errors, validation, etc.)
│   │   └── deps.py                       # Dependency injection (settings, S3, DynamoDB, validators)
│   │
│   ├── clients/
│   │   ├── s3.py                         # Long-lived async S3 clients (aioboto3)
│   │   └── dynamodb.py                   # Long-lived async DynamoDB client (aioboto3)
│   │
│   ├── modules/                          # Feature modules (routers + services)
│   │   ├── health/
│   │   │   └── router.py                 # GET /health
│   │   │
│   │   ├── uploads/
│   │   │   ├── router.py                 # POST /uploads (init, sign, list, complete)
│   │   │   ├── service.py                # Upload orchestration (multipart logic)
│   │   │   └── schemas.py                # Request/response models
│   │   │
│   │   └── videos/
│   │       ├── router.py                 # GET/POST/DELETE /videos, /dash streaming
│   │       ├── service.py                # Stream DASH files (range support)
│   │       ├── catalog.py                # List, get, hide, restore, delete videos
│   │       └── schemas.py                # VideoDetail, VideoListData, etc.
│   │
│   ├── repositories/
│   │   ├── video_repository.py           # Async VideoRepository (DynamoDB, async)
│   │   └── video_repository_sync.py      # Sync VideoRepository (Celery workers)
│   │
│   ├── workers/
│   │   ├── celery_app.py                 # Celery app config (broker, serializer)
│   │   ├── tasks.py                      # process_video task (lease, retry logic)
│   │   ├── processing.py                 # Orchestrate S3 + ffmpeg (download, transcode, upload)
│   │   └── transcode.py                  # ffmpeg DASH command builder (pure FS)
│   │
│   └── data/
│       └── categories.json               # Predefined category catalog (single source of truth)
│
├── tests/
│   ├── conftest.py                       # pytest fixtures (aiomoto S3/DynamoDB mocks)
│   ├── common/
│   │   └── test_errors.py                # Exception handler tests
│   └── modules/
│       ├── health/
│       │   └── test_health.py            # Health check tests
│       └── uploads/
│           └── test_router.py            # Upload flow tests
│
├── scripts/
│   └── export_openapi.py                 # Generate openapi.json / openapi.yaml
│
├── pyproject.toml                        # uv project manifest, deps, pytest config
├── Dockerfile                            # uv-based, includes ffmpeg + curl
└── .env.example                          # Template (AWS_ACCESS_KEY_ID, etc.)
```

### Layering

- **Router** (thin): validates path params, calls service
- **Service** (business logic): orchestrates dependencies, returns DTOs
- **Repository** (data access): CRUD on DynamoDB, atomicity via ConditionExpression
- **Clients** (I/O): long-lived S3 & DynamoDB connections

## Data Model

### DynamoDB Table: `video-metadata`

**Primary Key:** `video_key` (string, UUID4 hex, e.g. `a1b2c3d4e5f6...`)

**Attributes:**

| Name | Type | Purpose |
|------|------|---------|
| `video_key` | String | PK; server-generated, never from user input |
| `upload_id` | String | S3 multipart upload ID (for resuming) |
| `title` | String | User-supplied video title |
| `category` | String | One of the predefined categories from `categories.json` |
| `original_filename` | String | Client filename at upload |
| `size` | Number | Total file size in bytes |
| `content_type` | String | e.g. `video/mp4` |
| `status` | String | `uploading \| queued \| processing \| ready \| failed` |
| `created_at` | String | ISO 8601 timestamp (UTC) |
| `updated_at` | String | ISO 8601 timestamp (UTC) |
| `duration_seconds` | Number | (optional) Set when ready; ffprobe result |
| `resolutions` | List<String> | (optional) e.g. `["1080p", "720p", "480p"]` when ready |
| `error_message` | String | (optional) Set on failure; client-safe error text |
| `locked_until` | String | (optional) ISO 8601; lease expiry during processing |
| `visibility` | String | `active \| inactive` (for soft delete; powers the all-videos GSI) |

**Global Secondary Indexes:**

1. **`category-created_at-index`**
   - Partition: `category` (String)
   - Sort: `created_at` (String, descending = newest first)
   - Projection: All
   - Use case: Browse videos by category

2. **`visibility-created_at-index`**
   - Partition: `visibility` (String, `active` or `inactive`)
   - Sort: `created_at` (String, descending)
   - Projection: All
   - Use case: List all videos (active partition) or browsing all-videos feed

### S3 Layout

All S3 objects live under a video-specific prefix:

```
s3://videos/
└── uploads/{video_key}/
    ├── raw                              # Original uploaded file
    └── dash/                            # DASH artifacts (after transcoding)
        ├── manifest.mpd                 # DASH manifest (XML)
        ├── init-stream0.m4s             # Init segment (audio)
        ├── init-stream1.m4s             # Init segment (video)
        ├── chunk-stream0-00001.m4s      # Audio chunk
        ├── chunk-stream1-00001.m4s      # Video chunk (1080p)
        ├── chunk-stream1-00002.m4s      # ...
        └── ...
```

DASH output is produced by ffmpeg's `-f dash` with `-use_timeline 1 -use_template 1`.

## API Reference

All responses use the envelope: `{success: bool, data: T | null, error: string | null}`

### Health Module

#### GET `/api/health`
Health check. Always returns 200 OK.

**Response:**
```json
{
  "success": true,
  "data": {
    "status": "ok"
  },
  "error": null
}
```

### Uploads Module

#### POST `/api/uploads`
Initiate a resumable multipart upload.

**Request:**
```json
{
  "filename": "video.mp4",
  "content_type": "video/mp4",
  "size": 1073741824,
  "title": "My Video",
  "category": "movies"
}
```

**Validations:**
- `content_type` must be in `allowed_upload_content_types` (default: `video/mp4,video/quicktime,video/x-matroska,video/webm`)
- `size` must be ≤ `max_upload_bytes` (default: 5 GiB)
- `category` must be in the predefined catalog

**Response (201 Created):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6...",
    "upload_id": "s3-multipart-id",
    "part_size": 16777216,
    "part_count": 64
  },
  "error": null
}
```

**Status Codes:**
- 201: Success
- 413: File exceeds max size
- 415: Unsupported media type
- 422: Validation error (invalid category, size, etc.)

---

#### POST `/api/uploads/{video_key}/parts:sign`
Get presigned PUT URLs for a batch of part numbers.

**Request:**
```json
{
  "upload_id": "s3-multipart-id",
  "part_numbers": [1, 2, 3, 4, 5]
}
```

**Validations:**
- Part numbers must be 1–10,000 (S3 limits)
- Max 1,000 parts per request
- Part numbers must be unique

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "urls": {
      "1": "https://s3.../uploads/key/raw?partNumber=1&...",
      "2": "https://s3.../uploads/key/raw?partNumber=2&...",
      "3": "...",
      "4": "...",
      "5": "..."
    },
    "expires_in": 900
  },
  "error": null
}
```

Each URL is valid for 15 minutes (configurable via `presign_expiry_seconds`).

**Status Codes:**
- 200: Success
- 404: Video not found
- 422: Invalid video_key or part numbers

---

#### GET `/api/uploads/{video_key}/parts?upload_id={upload_id}`
List parts already received by S3. Used to resume an interrupted upload.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "parts": [
      {"part_number": 1, "etag": "abc123...", "size": 16777216},
      {"part_number": 2, "etag": "def456...", "size": 16777216},
      {"part_number": 3, "etag": "ghi789...", "size": 16777216}
    ],
    "part_count": 64
  },
  "error": null
}
```

**Status Codes:**
- 200: Success
- 404: Video not found
- 422: Invalid upload_id

---

#### POST `/api/uploads/{video_key}/complete`
Finalize the multipart upload after all parts are uploaded.

**Request:**
```json
{
  "upload_id": "s3-multipart-id",
  "parts": [
    {"part_number": 1, "etag": "abc123..."},
    {"part_number": 2, "etag": "def456..."},
    ...
  ]
}
```

**Validations:**
- All expected part numbers (1 to `part_count`) must be provided
- Part ETags must match the uploaded parts (S3 verification)

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6..."
  },
  "error": null
}
```

After this succeeds:
- The raw upload is finalized in S3
- The video record's status transitions from `uploading` → `queued`
- A `process_video` Celery task is enqueued

**Status Codes:**
- 200: Success
- 404: Video not found
- 422: Parts mismatch (missing, extra, or mismatched ETags)

### Videos Module

#### GET `/api/videos?category={cat}&status={st}&limit=20&cursor={c}`
List videos with optional filtering and cursor pagination.

**Query Parameters:**
- `category` (optional, string): Filter by category (e.g. `movies`)
- `status` (optional, string): Filter by status (`uploading`, `queued`, `processing`, `ready`, `failed`)
- `limit` (optional, int, default 20, max 100): Results per page
- `cursor` (optional, string): Opaque pagination cursor from a previous response

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "video_key": "a1b2c3d4e5f6...",
        "title": "My Video",
        "category": "movies",
        "status": "ready",
        "created_at": "2024-01-15T10:30:00+00:00",
        "duration_seconds": 120.5,
        "resolutions": ["1080p", "720p", "480p"]
      },
      ...
    ],
    "next_cursor": "eyJwayI6ICJhMWIyYzNkNCI..."
  },
  "error": null
}
```

Only videos with `visibility = "active"` are returned (soft-deleted videos are hidden).

**Status Codes:**
- 200: Success
- 422: Invalid filter or cursor

---

#### GET `/api/videos/categories`
List the predefined category catalog.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "categories": ["movies", "music", "sports", "education", "gaming", "news", "technology", "entertainment", "other"]
  },
  "error": null
}
```

---

#### GET `/api/videos/{video_key}`
Fetch full metadata for a single video.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6...",
    "title": "My Video",
    "category": "movies",
    "status": "ready",
    "created_at": "2024-01-15T10:30:00+00:00",
    "updated_at": "2024-01-15T10:35:00+00:00",
    "original_filename": "video.mp4",
    "size": 1073741824,
    "duration_seconds": 120.5,
    "resolutions": ["1080p", "720p", "480p"],
    "error_message": null,
    "manifest_path": "/api/videos/a1b2c3d4e5f6.../dash/manifest.mpd"
  },
  "error": null
}
```

`manifest_path` is only present when `status = "ready"`. Use it to initialize the dash.js player.

**Status Codes:**
- 200: Success
- 404: Video not found

---

#### POST `/api/videos/{video_key}/hide`
Soft delete: hide the video from listings without removing S3 objects.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6...",
    "visibility": "inactive"
  },
  "error": null
}
```

After hiding, the video does not appear in list or category browsing. It can be restored.

**Status Codes:**
- 200: Success
- 404: Video not found

---

#### POST `/api/videos/{video_key}/restore`
Undo a soft delete: make the video visible in listings again.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6...",
    "visibility": "active"
  },
  "error": null
}
```

**Status Codes:**
- 200: Success
- 404: Video not found

---

#### DELETE `/api/videos/{video_key}`
Hard delete: permanently remove the video's S3 objects and metadata.

**Response (200 OK):**
```json
{
  "success": true,
  "data": {
    "video_key": "a1b2c3d4e5f6...",
    "deleted": true
  },
  "error": null
}
```

**Status Codes:**
- 200: Success
- 404: Video not found
- 409: Video is still processing (hide it instead)

---

#### GET `/api/videos/{video_key}/dash/{filename}`
Stream a DASH artifact (manifest or segment) from S3 with range-request support.

**Path Parameters:**
- `video_key`: UUID4 hex (validated)
- `filename`: DASH artifact name (e.g. `manifest.mpd`, `chunk-stream1-00001.m4s`)

**Headers:**
- `Range` (optional): HTTP range request, e.g. `Range: bytes=0-1023`

**Response (200 OK or 206 Partial Content):**

The backend streams the file directly from S3. Response headers include:
- `Content-Type`: `application/dash+xml` for `.mpd`, `video/iso.segment` for `.m4s`
- `Accept-Ranges`: `bytes` (always present)
- `Cache-Control`: `no-cache` for `.mpd`, `public, max-age=86400, immutable` for segments
- `Content-Range` (if Range header was provided)

**Status Codes:**
- 200: Full file
- 206: Partial content (range request)
- 404: Video or file not found
- 416: Invalid range

The `.mpd` file references segments by relative path, so the backend path-based routing allows dash.js to resolve segment URLs transparently.

## Upload Flow

A resumable S3 multipart upload is a 4-step handshake between the client and the backend:

### Step 1: Initiate
**Client sends:** filename, content_type, size, title, category
**Backend does:**
- Validate content type and size
- Generate a new `video_key` (UUID4 hex)
- Compute part size and part count (respecting S3 limits)
- Call S3 `create_multipart_upload`
- Store a `VideoRecord` in DynamoDB with status `uploading`
**Backend returns:** video_key, upload_id, part_size, part_count

### Step 2: Sign Parts (repeat as needed)
**Client sends:** video_key, upload_id, list of part_numbers to upload
**Backend does:**
- For each part number, generate a presigned PUT URL valid for 15 minutes
- URLs point to `s3://{bucket}/uploads/{video_key}/raw?partNumber={n}&...`
**Backend returns:** map of part_number → presigned URL, expires_in seconds

The client can sign parts in batches as it downloads them, or call this multiple times if a batch expires.

### Step 3: Check Progress (optional)
**Client sends:** video_key, upload_id
**Backend does:**
- Query S3 for all parts already received
**Backend returns:** list of uploaded parts with ETags and sizes

The client uses this to skip parts already uploaded, enabling resumption after a network failure.

### Step 4: Complete
**Client sends:** video_key, upload_id, list of all parts (part_number + ETag for each)
**Backend does:**
- Validate that all expected parts (1 to `part_count`) are present
- Validate that submitted ETags match S3 records
- Call S3 `complete_multipart_upload`
- Transition video status: `uploading` → `queued`
- Enqueue a `process_video` Celery task
**Backend returns:** video_key

The raw upload is now finalized in S3 at `uploads/{video_key}/raw`.

## Transcoding Pipeline

Once a video is `queued`, the Celery worker claims and processes it:

### Claim (Lease-Based)
Worker calls `claim_for_processing(video_key, locked_until, updated_at)`:
- Uses a DynamoDB `ConditionExpression`: status must be `queued` OR status is `processing` AND `locked_until < now`
- Atomically transitions to `processing` and sets `locked_until` to 10 minutes from now
- If the condition fails (another worker holds the lease), the task retries after the lease window expires

This ensures only one worker processes a video at a time, and stale leases are reclaimed.

### Process
While processing:
- Download the raw file from S3 to a temp directory
- Run `ffprobe` to determine source height, duration, and audio presence
- Build an ffmpeg DASH command with the appropriate resolution ladder (1080p/720p/480p, skipping rungs above the source height)
- Run ffmpeg; it produces `manifest.mpd` and segments (`.m4s` files)
- Parse the result to extract actual resolutions and duration
- Upload all DASH artifacts to `uploads/{video_key}/dash/` in parallel (8 threads)

### Lease Renewal
A background thread renews the lease every 2 minutes (configurable) for as long as processing runs. If renewal fails, the task logs a warning but continues (the lease may have been taken by another worker, or the record may have been deleted).

### Success Path
When ffmpeg completes successfully:
- Call `mark_ready(video_key, resolutions=[], duration_seconds=123.45)`
- Atomically transitions status to `ready`, sets resolutions and duration, clears the lease

### Failure Path
If an exception occurs:
- If retries are exhausted (max 3, with 30-second backoff between retries), call `update_status(video_key, FAILED, error_message=str(exc))` and return
- Otherwise, call `release_to_queued(video_key)` to reset status and drop the lease, then retry

### Soft Time Limit
Tasks have a 1-hour soft time limit. If exceeded, a `SoftTimeLimitExceeded` exception is raised, caught, and the video is marked `failed` with the error `"processing timed out"`.

## Configuration

All settings are defined in `app/core/config.py` and loaded from environment variables or `.env` file.

| Setting | Type | Default | Purpose |
|---------|------|---------|---------|
| `app_name` | string | `StreamForge API` | FastAPI title |
| `environment` | string | `development` | Environment label (for logging) |
| `log_level` | string | `INFO` | Logging level |
| `api_prefix` | string | `/api` | Base path for all routes |
| `backend_cors_origins` | string | `` | Comma-separated CORS origins (empty = disabled) |
| `data_dir` | Path | `../data` | (Legacy; not actively used) |
| `celery_broker_url` | string | `redis://localhost:6379/0` | Celery broker (must be a valid Redis URL) |
| `celery_result_backend` | string | `redis://localhost:6379/1` | Celery result backend (must be valid) |
| | | | |
| **AWS / S3** | | | |
| `aws_endpoint_url` | string | `http://localhost:4566` | S3 server endpoint (server-side). Empty → real AWS |
| `aws_public_endpoint_url` | string | `http://localhost:4566` | S3 endpoint for presigned URLs (browser). Empty → real AWS |
| `aws_default_region` | string | `us-east-1` | AWS region |
| `aws_access_key_id` | string | **(required)** | AWS credentials (fail-fast if missing) |
| `aws_secret_access_key` | string | **(required)** | AWS credentials (fail-fast if missing) |
| `s3_bucket` | string | `videos` | S3 bucket name |
| `s3_use_path_style` | bool | `True` | S3 addressing style (path-style for the local emulator, virtual-hosted for real AWS) |
| | | | |
| **DynamoDB** | | | |
| `aws_dynamodb_endpoint_url` | string | `http://localhost:4566` | DynamoDB endpoint. Empty → real AWS |
| `dynamodb_video_table` | string | `video-metadata` | DynamoDB table name |
| | | | |
| **Upload Limits** | | | |
| `max_upload_bytes` | int | `5_368_709_120` (5 GiB) | Max file size per upload |
| `multipart_part_size` | int | `16_777_216` (16 MiB) | Preferred S3 multipart chunk size |
| `allowed_upload_content_types` | string | `video/mp4,video/quicktime,video/x-matroska,video/webm` | Allowed MIME types |
| `presign_expiry_seconds` | int | `900` (15 min) | Presigned URL lifetime |
| | | | |
| **Categories** | | | |
| `categories_file` | Path | `app/data/categories.json` | JSON file with allowed categories |
| | | | |
| **Transcoding** | | | |
| `ffmpeg_path` | string | `ffmpeg` | Path to ffmpeg binary |
| `ffprobe_path` | string | `ffprobe` | Path to ffprobe binary |
| | | | |
| **Worker Leasing** | | | |
| `worker_lease_seconds` | int | `600` (10 min) | Job lease duration |
| `worker_lease_renew_interval` | int | `120` (2 min) | How often to renew the lease |

### Production vs. local emulator

**Local emulator (development, compose service `aws`):**
```bash
aws_endpoint_url=http://aws:4566                # server-side, inside compose
aws_public_endpoint_url=http://localhost:4566   # browser-reachable, for presigned URLs
aws_dynamodb_endpoint_url=http://aws:4566
s3_use_path_style=true
```

**Real AWS (production):**
```bash
aws_endpoint_url=                      # empty → use real regional endpoint
aws_public_endpoint_url=               # empty → use real regional endpoint
aws_dynamodb_endpoint_url=             # empty → use real regional endpoint
s3_use_path_style=false                # real S3 prefers virtual-hosted style
# Provide real credentials + region + bucket
aws_access_key_id=<real key>
aws_secret_access_key=<real secret>
aws_default_region=us-east-1           # (or your region)
s3_bucket=<your-bucket-name>
dynamodb_video_table=video-metadata
```

## Running Locally

### Full Stack (Docker Compose)

```bash
docker compose up --build
```

Starts:
- **Backend**: http://localhost:8000 (uvicorn with reload)
- **Frontend**: http://localhost:5173 (Vite dev server)
- **Redis**: localhost:6379 (Celery broker + result backend)
- **AWS emulator** (moto server): localhost:4566 (S3 + DynamoDB); `aws-init` creates the bucket + table
- **Celery Worker**: processes videos (no exposed port)

All services read from `backend/.env` and `backend/.env.example`.

### Manual Development (No Compose)

Start dependencies separately:

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: AWS emulator + bucket/table provisioning (or use real AWS)
docker compose up aws aws-init -d

# Terminal 3: Backend (from backend/ directory)
export PYTHONPATH=.
uvicorn app.main:app --reload --port 8000

# Terminal 4: Celery worker (from backend/ directory)
celery -A app.workers.celery_app:celery_app worker --loglevel=info

# Terminal 5: Frontend (from frontend/ directory)
pnpm install && pnpm dev
```

### Dependencies via uv

```bash
cd backend
uv sync                # Install all deps
uv sync --no-dev       # Production only (no pytest, ruff, etc.)
```

## Testing

### Run Tests

```bash
cd backend
pytest                                 # Run all tests
pytest -v                              # Verbose
pytest tests/modules/uploads/          # Specific module
pytest -k "sign_parts"                 # By test name pattern
pytest --cov=app                       # Coverage report
```

### Test Coverage

Current coverage is partial:

| Module | Status |
|--------|--------|
| **Health** | Covered (test_health.py) |
| **Errors** | Covered (test_errors.py) |
| **Uploads Router** | Covered (test_router.py) |
| **Videos Module** | Not yet covered |
| **Worker** | Not yet covered |

Target: 80%+ coverage for all modules.

### Test Fixtures

Tests use `aiomoto` mocks (in-memory S3 + DynamoDB). See `conftest.py` for the `aws_override` fixture, which:
- Mocks S3 + DynamoDB via `aiomoto`
- Injects them into the app via `dependency_overrides`
- Creates the test table with GSIs

## API Contract Export

Generate OpenAPI schema for frontend integration:

```bash
cd backend
python scripts/export_openapi.py
```

Produces:
- `openapi.json` — JSON OpenAPI schema
- `openapi.yaml` — YAML OpenAPI schema

These can be imported into Swagger UI, ReDoc, or code generators. The frontend should reference the `servers` entries to route requests correctly.

## Conventions

### Response Envelope

All endpoints return:
```json
{
  "success": true,
  "data": { ... } or null,
  "error": "..." or null
}
```

- `success = true` → HTTP 2xx, `data` is populated, `error = null`
- `success = false` → HTTP 4xx/5xx, `data = null`, `error` is a string

### Path Safety

Every route that takes a `video_key` or DASH `filename` validates it strictly:
- `video_key` must match `^[0-9a-f]{32}$` (UUID4 hex)
- DASH filename must not contain `..` or path separators
- Invalid values return 422 Unprocessable Content

The routers use dependency-injected validators (`ValidVideoKey`, `ValidDashFilename` from `deps.py`) to enforce this before the handler sees the value.

### Atomicity

All DynamoDB writes use `ConditionExpression` to prevent concurrent clobbering:
- `claim_for_processing`: only if `status = queued` OR `(status = processing AND locked_until < now)`
- `set_visibility`: only if `video_key` exists
- `mark_ready`: only if `video_key` exists

If a condition fails, a `ConditionalCheckFailedException` is caught and the operation returns `False` (or raises, depending on context).

### Error Handling

Global exception handlers (`errors.py`) map exceptions to standard response envelopes:
- **Validation errors** → 422 + detailed error text (field paths)
- **S3 errors** → mapped via `_S3_ERROR_MAP` (InvalidPart → 422, NoSuchKey → 404, InvalidRange → 416)
- **HTTPException** → mapped to status code + detail
- **Unhandled exceptions** → 500 + `"Internal server error"` (full details logged server-side)

Secrets and internal paths are never leaked in error messages.

### Logging

Logger names follow the module path: `app.clients.s3`, `app.workers.tasks`, etc. Configured via `log_level` setting (default INFO).

Key logs:
- `app.workers.tasks`: lease claims, renewals, retries
- `app.workers.processing`: S3 downloads, transcoding, DASH uploads
- `app.workers.transcode`: ffprobe/ffmpeg commands and failures
- `app.clients.s3` / `app.clients.dynamodb`: client lifecycle

### Type Hints

All functions use full type hints. Async functions return `Coroutine[Any, Any, T]` or use `async def` syntax. Repositories and services are typed with `Annotated` dependency hints for FastAPI.

### Code Organization

- **Lines per file:** Target <800 (max)
- **Lines per function:** Target <50 (max)
- **Imports:** Sorted via ruff (handles isort style)
- **Formatting:** Validated by ruff (100-char line length)

## Common Tasks

### Add a New Endpoint

1. Create a new route function in the appropriate router (`modules/{feature}/router.py`)
2. Use dependency injection (`SettingsDep`, `S3ClientDep`, etc.) for service dependencies
3. Return an `ApiResponse[YourDataModel]`
4. Add tests in `tests/modules/{feature}/`

### Debug a Failing Job

1. Check the video record in DynamoDB: `status`, `error_message`, `locked_until`
2. Check Celery logs: `celery -A app.workers.celery_app:celery_app worker --loglevel=debug`
3. Check S3 for partial DASH uploads: `aws s3 ls s3://videos/uploads/{video_key}/dash/ --endpoint-url=http://localhost:4566`
4. If stuck on `processing` with an expired lease, manually reset via the repository

### Increase Upload Size Limit

Update `max_upload_bytes` in `config.py` or `.env`. Restart the backend.

### Add a New Category

Edit `app/data/categories.json` and restart the backend (the catalog is cached at startup).

### Monitor Job Queue

```bash
# From backend/
python -c "from app.workers.celery_app import celery_app; print(celery_app.control.inspect().active())"
```

Or use Flower (web UI for Celery):
```bash
pip install flower
flower -A app.workers.celery_app --port=5555
```

## Related Documentation

- API contract: the generated `openapi.json` / `openapi.yaml` in this directory
- Frontend client: see [`../frontend/README.md`](../frontend/README.md)
- Infrastructure: see [`../infra/terraform/README.md`](../infra/terraform/README.md)

## Summary

The backend is a FastAPI service for video uploads, transcoding, and streaming, built with security, scalability, and low-latency delivery in mind:

- **Uploads:** Resumable S3 multipart (4-step handshake)
- **Transcoding:** Async Celery with lease-based claim semantics
- **Catalog:** DynamoDB with soft/hard delete, category and full-feed browsing
- **Streaming:** DASH manifests + segments with range-request support
- **Configuration:** Environment-driven; runs on a local AWS emulator for dev, AWS-ready for prod

All code is typed, tested (partial coverage so far), and follows immutability and atomicity patterns for safe concurrency.

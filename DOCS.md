# DOCS.md — DocIntel Technical Reference

> Full technical documentation for the multi-tenant RAG platform. Written for both human developers and AI agents picking up this codebase.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Service Map](#2-service-map)
3. [Database Schema](#3-database-schema)
4. [API Reference](#4-api-reference)
5. [Ingestion Pipeline — Deep Dive](#5-ingestion-pipeline--deep-dive)
6. [Authentication System](#6-authentication-system)
7. [Frontend Architecture](#7-frontend-architecture)
8. [Docker & Infrastructure](#8-docker--infrastructure)
9. [Known Issues & Gotchas](#9-known-issues--gotchas)
10. [What Is Not Yet Implemented](#10-what-is-not-yet-implemented)
11. [Next Steps — Implementation Roadmap](#11-next-steps--implementation-roadmap)

---

## 1. Architecture Overview

DocIntel follows a **worker-queue-separated architecture**. The FastAPI backend is stateless and returns immediately after firing a task. All heavy ML work (document parsing, embedding) happens asynchronously in a dedicated Celery worker process that shares a Docker volume with the backend for file access.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Next.js    │────▶│  FastAPI    │────▶│  Redis      │
│  Frontend   │     │  Backend    │     │  (broker)   │
│  :3000      │     │  :8000      │     │  :6379      │
└─────────────┘     └──────┬──────┘     └──────┬──────┘
                           │                   │
                    ┌──────▼──────┐     ┌──────▼──────┐
                     │ PostgreSQL  │     │  Celery      │
                     │ + pgvector  │◀────│  Worker      │
                     │ + pg_search │     │  (ML tasks)  │
                    │  :5432      │     │  (ML tasks)  │
                    └─────────────┘     └─────────────┘
                           ▲
                    ┌──────┴──────┐
                    │  pgAdmin    │
                    │  :5050      │
                    └─────────────┘
```

**Key design decisions:**
- The Celery worker and backend share the same Docker image (built from `backend/Dockerfile`) but run different commands
- Files are written to a named Docker volume (`raw_uploads_data`) mounted at `/raw_uploads` in both containers — this is how the worker can read uploaded files without an S3 dependency
- The database uses **two different connection strategies**: psycopg2 (sync, used in Celery tasks) and SQLAlchemy async (used in FastAPI routes)

---

## 2. Service Map

| Container | Image | Command | Ports | Role |
|---|---|---|---|---|
| `rag_postgres` | `paradedb/paradedb:latest` | — | `5432` | PostgreSQL 17 + pgvector + pg_search (ParadeDB BM25) |
| `rag_redis` | `redis:7-alpine` | — | `6379` | Celery message broker + result backend |
| `rag_backend` | `./backend` Dockerfile | `uvicorn app.main:app --reload` | `8000` | FastAPI REST API |
| `rag_celery_worker` | `./backend` Dockerfile | `celery -A app.tasks.celery_app worker` | — | Async document ingestion worker (8 prefork processes) |
| `rag_frontend` | `./frontend` Dockerfile | `npm run dev` | `3000` | Next.js 15 UI |
| `rag_pgadmin` | `dpage/pgadmin4` | — | `5050` | Database GUI |

**Health checks:** `postgres` and `redis` have Docker health checks — the backend and worker wait for them before starting (`depends_on: condition: service_healthy`).

---

## 3. Database Schema

Database: PostgreSQL 17 with `pgvector` and `pg_search` (ParadeDB) extensions enabled.

All schema lives in `scripts/schema.sql`. The `parsed_documents` table was added via `scripts/migration_parsed_documents.sql`. The ParadeDB BM25 index is created via `scripts/migration_bm25.sql`.

### `tenants`

Stores tenant identities. One row per organisation.

```sql
CREATE TABLE tenants (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Notes:**
- Currently one test tenant is seeded manually: `id = 00000000-0000-0000-0000-000000000001`, `name = 'Test Tenant'`
- When auth is used, tenants are created automatically via `POST /api/auth/signup`

### `users`

Stores user accounts linked to tenants. (Created via the CRUD layer, schema managed by Alembic migrations.)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK → tenants | |
| `email` | TEXT UNIQUE | |
| `hashed_password` | TEXT | bcrypt |
| `is_active` | BOOLEAN | |
| `created_at` | TIMESTAMP | |

### `documents`

Tracks every uploaded document and its processing lifecycle.

```sql
CREATE TABLE documents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    storage_path TEXT,   -- /raw_uploads/<tenant_id>/<doc_id>_<filename>
    status       TEXT DEFAULT 'pending',
    created_at   TIMESTAMP DEFAULT NOW()
);
```

**Status lifecycle:** `pending` → `processing` → `completed` | `failed`

### `chunks`

Stores text chunks and their vector embeddings. This is the core table for hybrid search (vector + BM25).

```sql
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(1536),   -- OpenAI text-embedding-3-small dimension
    metadata    JSONB,          -- { "chunk_index": 0, "total_chunks": 42 }
    created_at  TIMESTAMP DEFAULT NOW()
);
```

**Indexes applied:**
```sql
CREATE INDEX idx_chunks_tenant ON chunks(tenant_id);
CREATE INDEX idx_chunks_document ON chunks(document_id);
```

**ParadeDB BM25 index** (created via `scripts/migration_bm25.sql`):
```sql
CREATE INDEX idx_chunks_bm25 ON chunks USING bm25 (id, content) WITH (key_field = 'id');
```

> ⚠️ The HNSW vector index is **not yet applied** (commented out in schema). Apply it after you have data: `CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);`

### `parsed_documents`

JSONB audit table. Stores the full parsed document structural JSON from PyMuPDF4LLM. Enables re-chunking strategies without re-parsing the original PDF.

```sql
CREATE TABLE parsed_documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    docling_json JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);
```

### `audit_logs`

Stores Q&A interactions for compliance and observability.

```sql
CREATE TABLE audit_logs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    action     TEXT,            -- e.g., 'query'
    details    JSONB,           -- { question, answer, chunks_used }
    created_at TIMESTAMP DEFAULT NOW()
);
```

> ⚠️ **Schema mismatch**: `ingestion_task.py` writes `question`, `answer`, `chunks_used` as top-level columns to `audit_logs`, but the current schema uses a `details JSONB` column. The query router uses the JSONB approach. Align these before production.

---

## 4. API Reference

Base URL: `http://localhost:8000`

All authenticated routes read the JWT from the `access_token` HttpOnly cookie.

### Auth

#### `POST /api/auth/signup`

Creates a new tenant + user account atomically.

**Request body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword",
  "organization_name": "Acme Corp"
}
```

**Response:** Sets `access_token` HttpOnly cookie. Returns:
```json
{
  "message": "Account created",
  "user_id": "<uuid>",
  "tenant_id": "<uuid>"
}
```

**Errors:** `400` email already registered | `400` org name taken

---

#### `POST /api/auth/login`

**Request body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword"
}
```

**Response:** Sets `access_token` HttpOnly cookie. Returns:
```json
{
  "message": "Login successful",
  "user_id": "<uuid>",
  "tenant_id": "<uuid>"
}
```

**Errors:** `401` invalid credentials | `403` account deactivated

---

#### `POST /api/auth/logout`

Clears the `access_token` cookie. JWT remains technically valid until expiry (stateless — no server-side revocation yet).

---

#### `GET /api/auth/me`

Returns the current user's identity from the cookie without hitting the database.

**Response:**
```json
{
  "user_id": "<uuid>",
  "tenant_id": "<uuid>"
}
```

---

### Upload

#### `POST /api/upload/`

**Auth:** Requires valid JWT cookie (or `X-Tenant-ID` header as fallback — used by the legacy test tenant).

**Request:** `multipart/form-data` with a `file` field.

**What it does:**
1. Generates a UUID for the document
2. Saves file to `/raw_uploads/<tenant_id>/<doc_id>_<filename>`
3. Inserts a row into `documents` with `status = 'pending'`
4. Fires `process_document_task.delay(document_id, tenant_id)` to Celery

**Response:**
```json
{
  "document_id": "<uuid>",
  "status": "pending",
  "filename": "report.pdf"
}
```

---

### Query

#### `POST /api/query/`

**Auth:** Requires valid JWT cookie.

**Request body:**
```json
{
  "question": "What are the key findings?"
}
```

**What it does:**
1. Embeds the question using **OpenAI `text-embedding-3-small`**
2. Runs **ParadeDB BM25** lexical search against `chunks` filtered by `tenant_id`
3. Runs **pgvector** cosine similarity search against `chunks` filtered by `tenant_id`
4. Fuses both result sets using **Reciprocal Rank Fusion** (RRF, k=60)
5. Builds context from top-7 fused chunks
6. Generates answer using **OpenAI `gpt-4o-mini`**
7. Logs to `audit_logs`

**Response:**
```json
{
  "answer": "[MOCK LLM]: ...",
  "sources": ["unknown"]
}
```

> ⚠️ The query endpoint is fully wired to OpenAI for embeddings and LLM. Real-world performance depends on API latency and token costs.

---

### Health

#### `GET /health`

```json
{ "status": "healthy" }
```

---

## 5. Ingestion Pipeline — Deep Dive

File: `backend/app/tasks/ingestion_task.py`

The Celery task `process_document_task` runs in a prefork worker (up to 8 concurrent). Each step is logged with the `document_id` prefix for easy filtering.

### Pipeline Steps

```
1. Fetch storage_path from documents table
        ↓
2. UPDATE documents SET status = 'processing'
        ↓
3. Run PyMuPDF4LLM on the file path
    - Extracts markdown text + structural JSON
         ↓
4. Serialize full parsed JSON → INSERT INTO parsed_documents (docling_json)
         ↓
5. Split markdown using MarkdownHeaderTextSplitter (by # ## ### headers)
    → RecursiveCharacterTextSplitter into fixed-size chunks
    - CHUNK_SIZE = 1500 chars
    - CHUNK_OVERLAP = 200 chars
         ↓
6. Call OpenAI `text-embedding-3-small` in batches of 128
    - Returns 1536-dimensional float embeddings
         ↓
7. Batch INSERT into chunks table (content, embedding::vector, metadata::jsonb)
         ↓
8. UPDATE documents SET status = 'completed'
```

### Error Handling

- Any exception triggers `conn.rollback()`, sets `status = 'failed'`, then calls `self.retry(exc=exc)`
- Max retries: **3** with **10 second** delay between each
- After 3 retries, the task raises and the document remains `status = 'failed'`

### Constants

```python
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"   # 1536-dim
CHUNK_SIZE = 1500                  # characters per chunk
CHUNK_OVERLAP = 200                # overlap between chunks
BATCH = 128                        # OpenAI API batch limit
```

### Known Issue: `cv2` / TableFormer

Docling's TableFormer model requires `opencv-python`. In early versions of the Dockerfile, `cv2` was missing, causing all ingestion tasks to fail. **Fix applied in the current Dockerfile:**

```dockerfile
RUN pip install -r requirements.txt && \
    pip uninstall -y opencv-python 2>/dev/null || true && \
    pip install --no-cache-dir opencv-python-headless==4.9.0.80 && \
    rm -rf /usr/local/lib/python3.12/site-packages/cv2/typing
```

The `cv2/typing` directory removal is critical — it shadows Python's built-in `typing` module during Uvicorn hot-reload and causes startup crashes.

---

## 6. Authentication System

### JWT Strategy

- Tokens are created with `python-jose` using HS256
- **Payload:** `{ "sub": "<user_id>", "tenant_id": "<tenant_id>", "exp": <timestamp> }`
- Expiry: **24 hours** (`COOKIE_MAX_AGE = 60 * 60 * 24`)
- Tokens are stored in **HttpOnly cookies** — JavaScript cannot read them (XSS protection)
- Cookie `secure=True` is only set when `ENV=production` (HTTPS required in production)

### Dependency Injection

`backend/app/api/dependencies.py` exports:

- `get_current_tenant_id(request)` — extracts `tenant_id` from JWT cookie, falls back to `X-Tenant-ID` header for legacy/test usage
- `get_current_user_id(request)` — extracts `sub` (user UUID) from JWT cookie

These are injected into route handlers via `Depends()`:

```python
@router.post("/")
async def upload_file(
    tenant_id: str = Depends(get_current_tenant_id),
    ...
):
```

### Password Hashing

`backend/app/auth/password.py` uses `bcrypt` via the `bcrypt` library:
- `hash_password(plain: str) -> str`
- `verify_password(plain: str, hashed: str) -> bool`

### Tenant + User Creation

`backend/app/crud/user.py::create_tenant_and_user` creates both records atomically in the same async DB session. If either insert fails, the whole transaction rolls back.

---

## 7. Frontend Architecture

Stack: **Next.js 15**, React 19, TypeScript, TailwindCSS

### Key Files

| File | Purpose |
|---|---|
| `app/page.tsx` | Main chat + upload interface. Wired to `POST /api/upload/`. Chat send button has a TODO block — not yet wired to `/api/query/` |
| `app/globals.css` | Full design system: CSS custom properties for dark/light mode, glassmorphism utilities |
| `app/layout.tsx` | Root layout — sets font, theme class, metadata |
| `components/ChatMessage.tsx` | Renders messages with role-based styling and citation display |
| `components/UploadZone.tsx` | File input button with upload spinner state |
| `components/StagedFile.tsx` | Shows selected file before the send action |
| `components/ThemeToggle.tsx` | Dark/light mode toggle using `next-themes` or CSS class toggling |
| `components/types.ts` | TypeScript interfaces: `Message`, `Citation`, `StagedFileInfo` |

### Environment

`NEXT_PUBLIC_API_URL=http://localhost:8000` — set in `docker-compose.yml` and `frontend/.env.local`. All fetch calls in the frontend should use this variable.

### Unfinished: Chat → Query Wiring

The chat send handler in `page.tsx` contains a `// TODO` block. To wire it:

```typescript
const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/query/`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  credentials: "include",          // sends the HttpOnly cookie
  body: JSON.stringify({ question: userMessage }),
});
const data = await res.json();
// data.answer, data.sources
```

---

## 8. Docker & Infrastructure

### Volumes

| Volume | Mount | Shared Between |
|---|---|---|
| `postgres_data` | `/var/lib/postgresql/data` | postgres only |
| `raw_uploads_data` | `/raw_uploads` | **backend + celery_worker** |
| `./backend` (bind) | `/app` | backend + celery_worker (hot-reload) |
| `./frontend` (bind) | `/app` | frontend |

> **Critical:** `raw_uploads_data` is what allows the Celery worker to read files uploaded through the API. Without this shared volume, ingestion would fail with file-not-found errors.

### Network

All services are on `rag_network` (bridge driver). Service names are DNS-resolvable: `postgres`, `redis`, `backend`, `frontend`.

### Backend Dockerfile Explanation

```dockerfile
FROM python:3.12-slim

# gcc + libpq-dev needed for psycopg2 compilation
RUN apt-get install -y gcc libpq-dev

# Install CPU-only PyTorch FIRST — prevents pulling 1.5GB CUDA build
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install requirements, then fix the OpenCV situation:
# 1. Docling's dependency tree pulls in opencv-python (needs libGL.so — not in slim image)
# 2. Uninstall it, reinstall headless version
# 3. Delete cv2/typing which shadows Python's typing module under hot-reload
RUN pip install -r requirements.txt && \
    pip uninstall -y opencv-python 2>/dev/null || true && \
    pip install opencv-python-headless==4.9.0.80 && \
    rm -rf /usr/local/lib/python3.12/site-packages/cv2/typing
```

---

## 9. Known Issues & Gotchas

### ParadeDB BM25 index requires `pg_search` extension

**Cause:** The ParadeDB Docker image includes `pg_search`, but the extension must be enabled per-database with `CREATE EXTENSION pg_search;`.

**Fix:** Run `scripts/schema.sql` which enables both `vector` and `pg_search`. If you see `ERROR: extension "pg_search" does not exist`, the ParadeDB image wasn't used — check `docker-compose.yml`.

### ParadeDB `paradedb:latest` tag

The `paradedb:latest` tag tracks the most recent release. For reproducible builds, consider pinning a specific version like `paradedb:0.25.3`.

### Celery worker running as root

The logs show: `SecurityWarning: You're running the worker with superuser privileges`. This is because the Docker container runs as root by default. Not a blocker for local dev, but **do not run as root in production**.

**Fix:** Add `USER appuser` to the Dockerfile (create the user first).

### `audit_logs` schema mismatch

`ingestion_task.py` (old version) tries to write `question`, `answer`, `chunks_used` as separate columns. The current schema stores everything under a `details JSONB` column. The query router uses JSONB correctly. The ingestion task no longer writes to `audit_logs` directly.

### Query endpoint uses OpenAI embeddings

`routers/query.py` embeds the question with OpenAI `text-embedding-3-small`, runs hybrid search (ParadeDB BM25 + pgvector with RRF), and uses GPT-4o-mini for answer generation. This is fully wired and returns meaningful results as long as documents have been ingested.

---

## 10. What Is Not Yet Implemented

| Feature | File | Status |
|---|---|---|
| Real embedding in query | `routers/query.py` | Fully wired to OpenAI |
| Real LLM call in query | `routers/query.py` | Fully wired to OpenAI GPT-4o-mini |
| Chat wired to backend | `frontend/app/page.tsx` | TODO block in send handler |
| Document status polling | `routers/status.py` | Empty placeholder |
| HNSW vector index | `scripts/schema.sql` | Commented out |
| `config.py` pydantic-settings | `app/config.py` | Empty placeholder |
| SQLAlchemy ORM models | `app/models/` | Empty directory |
| Supabase RLS | `scripts/init_supabase.sql` | Empty placeholder |
| JWT token revocation | — | Stateless JWTs only |
| Celery worker non-root user | Dockerfile | Runs as root |
| ParadeDB BM25 migration | `scripts/migration_bm25.sql` | Created, needs to be run against postgres |

---

## 11. Next Steps — Implementation Roadmap

### Step 1: Apply HNSW index

After you have documents ingested, run:

```sql
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
```

This changes similarity search from O(n) brute-force to approximate nearest-neighbour and is essential for production scale.

### Step 2: Wire frontend chat to `/api/query/`

In `frontend/app/page.tsx`, find the `// TODO` in the send handler and replace with a real fetch to `/api/query/` (see section 7).

### Step 3: Add document status polling

Implement `GET /api/status/{document_id}` in `routers/status.py`:

```sql
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
```

This changes similarity search from O(n) brute-force to approximate nearest-neighbour and is essential for production scale.

### Step 4: Add a chunking strategy choice

```python
@router.get("/{document_id}")
async def get_status(document_id: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT status FROM documents WHERE id = %s", (document_id,))
    row = cur.fetchone()
    return {"status": row["status"]}
```

Then poll from the frontend after upload to show real-time status updates.

### Step 5: Add a chunking strategy choice

The current fixed-size character chunker is simple but loses semantic boundaries. Consider upgrading to:
- **Sentence-level chunking** with `nltk.sent_tokenize`
- **Docling-native chunking** using the structural JSON in `parsed_documents` (headings, paragraphs, tables as natural boundaries)

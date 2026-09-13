# DocIntel — Multi-Tenant RAG Platform

> A multi-tenant Retrieval-Augmented Generation platform for building isolated document knowledge bases and querying them with AI.

## Overview

DocIntel is a multi-tenant RAG system that allows users to upload documents, build their own knowledge base, and ask questions about their documents through an AI-powered interface.

Each tenant has an isolated set of documents and data, allowing multiple users or organizations to use the same system without sharing their knowledge bases.


## Features

- Multi-tenant document workspaces with tenant-level data isolation
- Upload and query documents through a web interface
- Hybrid semantic and lexical retrieval
- Cross-encoder reranking for retrieved context
- AI-powered question answering with source references
- Asynchronous document processing

## Quick Start

### Prerequisites

* Docker Engine 24+ / Docker Desktop
* An OpenAI API key

### 1. Clone the repository

```bash
git clone <repository-url>
cd multi-tenant-rag
```

### 2. Configure

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=your_openai_key_here
JWT_SECRET_KEY=a-random-32-character-secret
```

### 3. Initialize the database

Start PostgreSQL:

```bash
docker compose up -d postgres
```

Then initialize the database:

```bash
docker exec -i rag_postgres psql -U postgres -d rag_db < scripts/schema.sql
docker exec -i rag_postgres psql -U postgres -d rag_db < scripts/migration_parsed_documents.sql
docker exec -i rag_postgres psql -U postgres -d rag_db < scripts/migration_bm25.sql
```

### 4. Start DocIntel

```bash
docker compose up -d --build
```

### 5. Use DocIntel

Open the web interface at http://localhost:3000.

1. Sign up to create your tenant account.
2. Upload a document and wait for ingestion to complete.
3. Ask questions about your documents through the chat interface.


## 📦 Technologies

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 15, React 19, TypeScript, TailwindCSS |
| Backend API | FastAPI (Python 3.12), Uvicorn |
| Task Queue | Celery 5.6, prefork workers |
| Message Broker | Redis 7 Alpine |
| Database | PostgreSQL 17 + pgvector + ParadeDB pg_search (BM25) |
| Document Parsing | PyMuPDF4LLM (markdown + JSON extraction) |
| Embeddings | OpenAI `text-embedding-3-small` (1536-dim) |
| Reranking | ONNX Runtime + BGE cross-encoder reranker (quantized INT8, ~280MB) |
| Auth | python-jose JWT, bcrypt, HttpOnly cookies |
| DB GUI | pgAdmin 4 |

---

## 🔧 Configuration

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | OpenAI embeddings + LLM |
| `JWT_SECRET_KEY` | ✅ | JWT signing secret (min 32 chars) |
| `DATABASE_URL` | Auto | Set by docker-compose |
| `REDIS_URL` | Auto | Set by docker-compose |
| `ENV` | ⬜ | Set to `production` for HTTPS-only cookies |

---

## ✅ Requirements

- Docker Engine 24+ or Docker Desktop
- Docker Compose v2
- 4 GB RAM recommended (Docling ML models)
- OpenAI API key

---

## 🗂️ Repository Structure

```
multi-tenant-rag/
├── docker-compose.yml           # 6-service orchestration
├── .env                         # Secrets — never commit
├── backend/
│   ├── Dockerfile               # Python 3.12-slim + ONNX reranker model
│   ├── requirements.txt
│   └── app/
│       ├── main.py              # FastAPI app + CORS + routers
│       ├── auth/                # JWT + bcrypt helpers
│       ├── api/
│       │   ├── dependencies.py  # get_current_tenant_id, get_current_user_id
│       │   └── routers/
│       │       ├── auth.py      # /signup /login /logout /me
│       │       ├── upload.py    # POST /upload/ → Celery
│       │       ├── query.py     # POST /query/ → BM25 + vector + ONNX rerank
│       │       └── documents.py # /documents list, retry, delete
│       ├── db/                  # psycopg2 sync + SQLAlchemy async sessions
│       ├── crud/                # user + tenant CRUD
│       ├── services/
│       │   └── reranker.py      # ONNX BGE cross-encoder reranker
│       └── tasks/
│           ├── celery_app.py    # Celery init + Redis broker
│           └── ingestion_task.py# PyMuPDF4LLM → chunk → embed → pgvector
├── frontend/
│   ├── Dockerfile               # Node 20 Alpine
│   ├── app/
│   │   ├── page.tsx             # Chat + upload interface (full-width, collapsible sidebar)
│   │   └── globals.css          # Design system (dark/light, glassmorphism)
│   └── components/              # ChatMessage, UploadZone, ThemeToggle, SourcesPanel, etc.
└── scripts/
    ├── schema.sql               # DB schema — run once
    ├── migration_parsed_documents.sql
    ├── migration_bm25.sql
    └── rag_cli/                 # Terminal CLI for upload/query/status
        ├── main.py              # Typer app
        ├── client.py            # HTTP client with cookie auth
        └── formatters.py        # Rich output formatting
```

---

## 🔗 Data Flow

```mermaid
graph TD
    A["👤 Browser"] -->|POST /api/upload| B["FastAPI"]
    B -->|save file| C["Docker Volume /raw_uploads"]
    B -->|INSERT status=pending| D["PostgreSQL"]
    B -->|task.delay| E["Redis"]
    E -->|pick up| F["Celery Worker"]
    F -->|read file| C
    F -->|PyMuPDF4LLM parse| G["Markdown + JSON"]
    G --> F
    F -->|INSERT docling_json| D
    F -->|chunk + embed| H["OpenAI API"]
    H --> F
    F -->|INSERT chunks+vectors| D
    A -->|POST /api/query| B
    B -->|hybrid search (BM25 + vector)| D
    D -->|top-K chunks| B
    B -->|ONNX rerank| F
    B -->|answer + sources| A
```

---

## 📄 Documentation

See [`DOCS.md`](./DOCS.md) for the full technical reference — API endpoints, database schema, ingestion pipeline breakdown, known issues, and what to build next.

---

## 📝 Changelog

| Date | Event |
|---|---|
| 2026-09-04 | Frontend revamp: full-width layout, collapsible sidebar (280px), smooth transitions |
| 2026-08-25 | Switched to ParadeDB for true BM25 lexical search (replaced PostgreSQL FTS `ts_rank`) |
| 2026-07-05 | Dockerfile fixed: PyTorch CPU + opencv-python-headless + cv2/typing removal |
| 2026-07-01 | Auth system: JWT HttpOnly cookies, signup/login/logout/me |
| 2026-06-29 | `parsed_documents` JSONB migration applied |
| 2026-06-29 | Full Celery ingestion pipeline (Docling → chunk → OpenAI → pgvector) |
| 2026-06-19 | Upload pipeline end-to-end verified |
| 2026-06-08 | Initial project scaffold |

---

## ❤️ Acknowledgements

- [PyMuPDF4LLM](https://github.com/ezwikix/pymupdf4llm) — PDF to markdown + JSON extraction
- [OpenAI](https://openai.com/) — embeddings and LLM completions
- [pgvector](https://github.com/pgvector/pgvector) — vector search inside PostgreSQL
- [ParadeDB](https://github.com/paradedb/paradedb) — true BM25 full-text search via `pg_search`
- [FastAPI](https://fastapi.tiangolo.com/) — ergonomic Python API framework

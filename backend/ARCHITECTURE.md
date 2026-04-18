# CurMath Flashcard Engine — Architecture & Deployment

RAG-based flashcard generator for NCERT/CBSE textbooks. Ingests PDFs, generates exam-aligned flashcards via LLMs, tracks per-user progress.

---

## Tech stack

| Layer | Choice |
|---|---|
| API | FastAPI + Uvicorn |
| Auth | Supabase Auth (JWT via JWKS) |
| Relational DB | Supabase Postgres |
| Vector DB | Qdrant Cloud |
| Embeddings | Gemini (`gemini-embedding-001`) |
| Generation | Gemini (`gemini-2.5-flash-lite`) |
| HyDE / query rewrite | Groq (`llama-3.3-70b-versatile`) |
| Reranker | LangSearch |
| PDF parsing | PyMuPDF + pdfplumber |
| Rate limiting | slowapi (in-memory) |

---

## Backend folder layout

```
backend/
├── app/
│   ├── main.py                    # FastAPI app + middleware wiring + route mounting
│   ├── api/endpoints/             # HTTP routers
│   │   ├── auth.py                # register / login / me / sync-profile / change-password / delete-account
│   │   ├── ingestion.py           # user PDF upload + source status
│   │   ├── sessions.py            # create / start / answer / finalize / history
│   │   ├── library.py             # system library (read: all users, write/delete: admin only)
│   │   └── users.py               # /users/me/activity heatmap
│   ├── services/                  # business logic (stateless, called from endpoints)
│   │   ├── auth_service.py        # Supabase admin API wrapper
│   │   ├── ingestion_service.py   # PDF → chunks → embeddings → Qdrant
│   │   ├── retrieval_service.py   # HyDE + dense + sparse (BM25) + RRF + rerank
│   │   ├── generation_service.py  # cards from chunks (LLM) + QA check (LLM)
│   │   └── session_service.py     # session lifecycle + report + topic stats
│   ├── core/
│   │   ├── config.py              # Settings (reads .env)
│   │   ├── security.py            # get_current_user_id, get_current_admin (JWT)
│   │   ├── middleware.py          # SecurityHeadersMiddleware, PayloadSizeMiddleware
│   │   └── rate_limit.py          # slowapi Limiter
│   └── db/
│       ├── supabase.py            # get_supabase() client factory
│       └── migrations/            # SQL migrations (run manually in Supabase SQL Editor)
├── data/uploads/                  # Runtime PDF storage (ephemeral on containers — needs a volume)
├── ARCHITECTURE.md                # this file
└── .env                           # (not committed) API keys + config
```

---

## Database schema (Supabase Postgres)

All tables are in `public`. Supabase Auth lives in the `auth` schema; `public.users` mirrors it.

### `public.users` — profile
| Column | Type | Notes |
|---|---|---|
| `user_id` | `uuid` PK | same as `auth.users.id` |
| `email` | `text` | |
| `name` | `text` | |
| `total_mastery_score` | `integer` | |
| `streak_days` | `integer` | |

### `public.sources` — uploaded PDFs (user uploads + system library)
| Column | Type | Notes |
|---|---|---|
| `source_id` | `text` PK | `src_...` (user) or `lib_...` (library) |
| `user_id` | `uuid` | null for `system_library` |
| `file_url` | `text` | disk path under `UPLOAD_DIR` |
| `file_name` | `text` | |
| `source_type` | `text` | `user_upload` \| `system_library` |
| `subject`, `board`, `chapter` | `text` | e.g. `Biology / CBSE / Photosynthesis` |
| `status` | `text` | `pending` → `processing` → `completed` \| `failed` |
| `chunk_count` | `int` | set at end of ingestion |
| `topics` | `jsonb` | array of topic names detected from headings |
| `processed_at`, `error_message` | | |
| `created_at` | `timestamptz` | |

### `public.sessions` — a single study session
| Column | Type | Notes |
|---|---|---|
| `session_id` | `uuid` PK | |
| `user_id` | `uuid` FK → users | `ON DELETE CASCADE` |
| `source_id` | `text` FK → sources | `ON DELETE CASCADE` |
| `num_cards` | `int` | 1–15 |
| `status` | `text` | `scheduled` → `in_progress` → `completed` \| `abandoned` |
| `scheduled_for` | `timestamptz` | nullable |
| `started_at`, `completed_at` | | |
| `final_report_json` | `jsonb` | also stashes `_focus_topics`, `_page_range` pre-start |

### `public.cards` — generated flashcards, bound to a session
| Column | Type | Notes |
|---|---|---|
| `card_id` | `text` PK | `card_...` |
| `session_id` | `uuid` FK → sessions | `ON DELETE CASCADE` |
| `user_id` | `uuid` FK → users | |
| `source_id` | `text` FK → sources | |
| `question`, `answer`, `hint` | `text` | |
| `card_type` | `text` | `long_answer` \| `mcq` \| `true_false` \| `spot_the_error` |
| `difficulty` | `text` | `Easy`, `Medium`, `Hard` |
| `topic`, `chapter`, `board`, `subject` | `text` | |
| `options` | `jsonb` | for MCQ only: `{"choices":["A: …","B: …"], "correct":"B"}` |

### `public.user_reviews` — one row per (user, card) answer (upserted)
| Column | Type | Notes |
|---|---|---|
| `review_id` | `uuid` PK | |
| `card_id` | `uuid` FK → cards | `ON DELETE CASCADE` |
| `session_id` | `uuid` FK → sessions | |
| `user_id` | `uuid` FK → users | |
| `user_answer` | `text` | |
| `is_correct` | `bool` | null for long-answer (graded at finalize) |
| `is_skipped` | `bool` | |
| `used_hint` | `bool` | |
| `created_at` | `timestamptz` | |
| `UNIQUE (user_id, card_id)` | | re-answers upsert |

### `public.user_topic_stats` — aggregates that drive weak-topic retests
| Column | Type | Notes |
|---|---|---|
| `user_id` | `uuid` FK | |
| `topic` | `text` | |
| `correct_count`, `incorrect_count`, `skipped_count`, `hinted_count` | `int` | bumped at session finalize |
| `last_seen_at` | `timestamptz` | |
| `PRIMARY KEY (user_id, topic)` | | |

### Qdrant — vector store
- Collection: `ncert_chunks`
- Vector size: `768` (Gemini embedding dim), distance: cosine
- Payload fields (all indexed as keyword except `page_num`):
  `chunk_id`, `text`, `topic`, `chapter`, `subject`, `board`, `class`, `source_id`, `page_num`

---

## How things work (request flow)

### PDF upload (`POST /api/v1/ingest/pdf`)
1. Magic-byte check (`%PDF-`) + size cap (25 MB) + field validation.
2. PDF streamed to disk under `UPLOAD_DIR`.
3. `sources` row inserted with `status=pending`.
4. `BackgroundTasks.add_task(IngestionService.ingest_pdf, ...)` fires and returns immediately.
5. Background task:
   - `status → processing`
   - Extract text (PyMuPDF) + tables (pdfplumber), tagged with `[PAGE:N]` markers.
   - Chunk into ~300-word paragraphs, detect topic from markdown-style `### heading ###`.
   - Batch-embed with Gemini → upsert `PointStruct` list into Qdrant.
   - `status → completed` (or `failed` with error message).

### Session lifecycle
1. `POST /sessions/create` — validates the source is yours (or is a `system_library` book). Writes `sessions.status=scheduled`.
2. `POST /sessions/{id}/start` — expensive: runs retrieval + generation inline (~10–30 s). Writes `cards` rows, flips to `in_progress`. Idempotent: re-hitting returns existing cards.
3. `POST /sessions/{id}/answer` per card — upserts a `user_reviews` row. `is_correct` computed deterministically for mcq/true_false; null for long-answer.
4. `POST /sessions/{id}/finalize` — one Gemini call grades all long-answers in a batch, builds `final_report_json`, bumps `user_topic_stats`, flips to `completed`.

### Retrieval pipeline (5 steps, used inside `/sessions/start`)
1. **HyDE**: Groq llama-3.3-70b writes a short NCERT-style explanation for the topic query.
2. **Dense search**: embed the HyDE text with Gemini → Qdrant ANN search, filtered by `board`, `subject`, `source_id`, optional `page_num` range.
3. **Sparse search**: BM25 over the same filtered scroll (Okapi, python impl).
4. **RRF fusion**: merge dense + sparse rankings (k=60).
5. **Rerank**: LangSearch API scores the top 20 against the query → return top 5.

### Auth
- Supabase Auth issues the JWT (ES256, audience `authenticated`).
- Backend verifies locally using Supabase's public JWKS (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`), cached in-process.
- **No JWT secret in env** — the backend never holds signing material.
- Admin endpoints also check the JWT's `email` claim against the `ADMIN_EMAILS` env allowlist.

### CORS
- Middleware order: `SecurityHeadersMiddleware` → `PayloadSizeMiddleware` → `CORSMiddleware` (outermost).
- Allowed methods: GET / POST / PUT / DELETE / OPTIONS.
- Allowed headers: `Authorization`, `Content-Type`.
- `CORS_ORIGINS` env var (comma-separated) or defaults to `http://localhost:3000`.

---

## Required environment variables

```
# Supabase
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...                  # the 'secret' / service_role key

# LLMs
GOOGLE_API_KEY=...                             # Gemini
GROQ_API_KEY=...                               # HyDE
LANGSEARCH_API_KEY=...                         # reranker

# Vector DB
QDRANT_URL=https://....cloud.qdrant.io
QDRANT_API_KEY=...

# Auth / gates
ADMIN_EMAILS=you@example.com,other@example.com # comma-separated
CORS_ORIGINS=https://your-frontend.com         # comma-separated; defaults to http://localhost:3000
```

---

## Deployment — AWS EC2

End-to-end recipe for a fresh Ubuntu 22.04 EC2 instance. Assumes you have the EC2 public IP and a domain you want to point at it.

### 1. Provision the instance
- AMI: Ubuntu 22.04 LTS, t3.small (2 GB RAM) or larger.
- Security Group inbound: open **22** (SSH, your IP), **80** (HTTP, 0.0.0.0/0), **443** (HTTPS, 0.0.0.0/0).
- Attach an Elastic IP so the public address survives restarts.
- SSH: `ssh -i key.pem ubuntu@<EIP>`

### 2. System deps
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12-venv python3-pip git nginx certbot python3-certbot-nginx
```

### 3. Clone and install
```bash
cd ~
git clone https://github.com/<you>/card-RAG.git
cd card-RAG
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 4. Write `.env` (at repo root)
```bash
nano .env
# paste all the variables from the "Required environment variables" section
chmod 600 .env
```

### 5. systemd service
```bash
sudo nano /etc/systemd/system/curmath.service
```
Paste (adjust `User` if you're not `ubuntu`):
```ini
[Unit]
Description=CurMath Flashcard Engine API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/card-RAG/backend
EnvironmentFile=/home/ubuntu/card-RAG/.env
ExecStart=/home/ubuntu/card-RAG/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable curmath
sudo systemctl start curmath
sudo systemctl status curmath   # expect "active (running)"
```

Logs: `journalctl -u curmath -n 100 --no-pager`.

### 6. nginx reverse proxy
```bash
sudo nano /etc/nginx/sites-available/curmath
```
Paste:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_connect_timeout   60s;
        proxy_send_timeout     180s;
        proxy_read_timeout     180s;
    }
}
```
Enable and test:
```bash
sudo ln -sf /etc/nginx/sites-available/curmath /etc/nginx/sites-enabled/curmath
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 7. DNS
Point your domain's A record at the EC2 Elastic IP. Verify with `dig +short your-domain.com` — must return the EIP.

### 8. TLS cert (Let's Encrypt)
```bash
sudo certbot --nginx -d your-domain.com
# Pick "redirect HTTP to HTTPS"
```
Auto-renewal is set up by default. Test with `sudo certbot renew --dry-run`.

### 9. Smoke test
```bash
curl -sS -i https://your-domain.com/
# → HTTP/2 200 {"message":"CurMath Flashcard Engine API v2..."}
```

### 10. Persistent uploads
`backend/data/uploads/` is on the instance's root volume — fine for EC2 (won't vanish on process restart) but **do attach an EBS snapshot schedule** so uploads survive an instance rebuild. For multi-instance / autoscaling later, move to S3 + signed URLs.

### Redeploy (after pushing code)
```bash
cd ~/card-RAG
git pull
sudo systemctl restart curmath
sudo systemctl status curmath
```

If you added Python packages, `~/card-RAG/.venv/bin/pip install -r requirements.txt` first.

---

## Known limitations (not blockers, but noted)

- **Rate limiter is in-memory.** If you scale to multiple workers or instances, counts won't be shared — switch slowapi to Redis.
- **Background ingestion has no retry.** If the process restarts mid-ingest, the source is left `status=processing` forever. Add a reconciliation job or move to Celery / SQS for real robustness.
- **`datetime.utcnow()`** is used in several places (deprecated in Python 3.13). Migrate to `datetime.now(timezone.utc)` when convenient.
- **Long-answer grading is batched**: one Gemini call at finalize-time grades all long answers together. If that call fails, users see zero-score fallbacks.

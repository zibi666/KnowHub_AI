# KnowHub AI

KnowHub AI is a private GPT-style web application for personal or small-team use. It includes a Vue chat UI, a FastAPI backend, encrypted per-user model API keys, OpenAI-compatible streaming, conversation history, attachment parsing, admin tools, usage accounting, and Docker Compose deployment.

## Features

- GPT-style chat with streaming responses, markdown, code highlighting, math rendering, and collapsible long user prompts.
- Per-user login, CSRF protection, session cookies, and first-login password reset.
- Encrypted user API keys with model discovery and active-key switching.
- Conversation history, soft-delete conversation list, context stats, and manual/automatic compaction.
- File attachments with local storage, text/PDF/DOCX parsing, image thumbnail support, and cleanup jobs.
- Admin pages for users, API key groups, quotas, analytics, runtime settings, cleanup, and dead letters.
- One-command Docker deployment with Redis, backend worker, FastAPI backend, and Nginx frontend gateway.

## Tech Stack

- Frontend: Vue 3, Vite, Pinia, Tailwind CSS, markdown-it, KaTeX, highlight.js.
- Backend: FastAPI, SQLAlchemy async, SQLite by default, Redis, ARQ, httpx.
- Deployment: Docker Compose, Nginx, Redis.

## Quick Start With Docker

Requirements:

- Docker and Docker Compose v2
- A model API key for an OpenAI-compatible endpoint

Clone the repository:

```bash
git clone git@github.com:zibi666/KnowHub_AI.git
cd KnowHub_AI
```

Create your environment file:

```bash
cp .env.example .env
```

Generate a fresh encryption key and put it into `APP_ENCRYPTION_KEY` in `.env`:

```bash
python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

Start the app:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

Default first login:

- Username: `admin`
- Password: `ChangeMe123!`

After login, change the temporary password, then add your model API key in Settings or API Management.

## Environment Variables

The most important values in `.env`:

```text
APP_ENCRYPTION_KEY=...
ADMIN_INITIAL_USERNAME=admin
ADMIN_INITIAL_PASSWORD=ChangeMe123!
MODEL_BASE_URL=https://nexor.nexoraivision.com/v1
MODEL_API_MODE=responses
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
REDIS_URL=redis://redis:6379/0
REDIS_SESSION_URL=redis://redis:6379/1
```

Do not commit `.env`, `data/`, `logs/`, or database files.

## Local Development

Backend:

```bash
cd backend
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

Open:

```text
http://localhost:5173
```

For local development without Docker, use Redis locally or update `REDIS_URL` and `REDIS_SESSION_URL` in `.env`.

## Useful Commands

Build frontend:

```bash
cd frontend
npm run build
```

Check backend syntax:

```bash
cd backend
python -m compileall app
```

View Docker logs:

```bash
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f nginx
```

Stop services:

```bash
docker compose down
```

Remove local runtime data:

```bash
docker compose down -v
rm -rf data logs
```

## Data Storage

By default, runtime data is stored under:

- `data/app.db`
- `data/local-storage`
- `data/cache`
- `logs/`
- Docker volume `redis-data`

These are intentionally ignored by Git.

## Model Provider

The backend calls an OpenAI-compatible API. Defaults are:

```text
MODEL_BASE_URL=https://nexor.nexoraivision.com/v1
MODEL_API_MODE=responses
```

If your provider only supports Chat Completions, set:

```text
MODEL_API_MODE=chat_completions
```

Then restart:

```bash
docker compose up -d --build
```

## Security Notes

- Change `ADMIN_INITIAL_PASSWORD` before exposing the service.
- Generate a unique `APP_ENCRYPTION_KEY` before adding API keys.
- Keep `.env` private. Existing encrypted API keys cannot be decrypted if you lose or change `APP_ENCRYPTION_KEY`.
- Put the service behind HTTPS for production.
- Review allowed upstream hosts with `MODEL_BASE_URL_ALLOWED_HOSTS`.

## Project Structure

```text
backend/   FastAPI app, models, services, workers
frontend/  Vue 3 SPA
nginx/     Production Nginx gateway and frontend build image
data/      Runtime data, ignored by Git
logs/      Runtime logs, ignored by Git
```

## License

Private project unless you add a license file.

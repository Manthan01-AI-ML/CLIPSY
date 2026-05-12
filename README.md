# ClipWise

AI-powered video repurposing platform. See `docs/MASTER_DOCUMENT.md` for full spec.

## Quick Start

```bash
# 1. Copy env (already done by scaffold)
cp .env.example .env    # then edit ANTHROPIC_API_KEY

# 2. Start everything
docker-compose up --build

# 3. Visit
# Backend API:  http://localhost:8000
# API docs:     http://localhost:8000/docs
```

## Stop

```bash
docker-compose down
```

## Reset DB

```bash
docker-compose down -v    # -v deletes volumes
docker-compose up --build
```

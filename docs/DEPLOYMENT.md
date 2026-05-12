# Deployment Runbook

---

## Template

```
## [Environment Name] (e.g., Local Dev / Staging / Production)

**URL:**
**Host / Provider:**
**Last deployed:**

### Prerequisites
- ...

### Steps
1. ...

### Environment variables required
| Variable | Description |
|---|---|
| ... | ... |

### Rollback
...

### Health checks
- API: GET /api/health
- DB: checked inside /api/health
- Redis: checked inside /api/health
```

---

<!-- Add deployment environments below this line -->

## Local Dev (Docker Compose)

**URL:** http://localhost:8000
**API docs:** http://localhost:8000/docs

### Steps
1. Copy env: `cp .env.example .env` and fill in `ANTHROPIC_API_KEY`, `GROQ_API_KEY`
2. `docker-compose up --build`
3. Visit http://localhost:8000

### Reset DB
```bash
docker-compose down -v
docker-compose up --build
```

### ngrok (for external access / webhooks)
See `NGROK_SETUP.md` in the project root.

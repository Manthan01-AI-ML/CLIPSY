# API Cost Tracking

Tracks real-world API costs as we test and scale.

---

## Pricing reference (as of 2026-05)

| Service | Model | Input | Output |
|---|---|---|---|
| Anthropic | claude-sonnet-4-6 | $3.00 / MTok | $15.00 / MTok |
| Groq | llama-3.3-70b-versatile | ~$0.59 / MTok | ~$0.79 / MTok |
| Groq | Whisper (transcription) | ~$0.111 / hr audio | — |
| Google | gemini-2.0-flash | ~$0.10 / MTok | ~$0.40 / MTok |

---

## Per-video cost estimate (Claude two-pass)

| Video length | Approx. input tokens | Approx. cost |
|---|---|---|
| 10 min | ~5,000 | ~$0.08 |
| 30 min | ~15,000 | ~$0.10 |
| 1 hr | ~30,000 | ~$0.12 |
| 4 hr | ~80,000 (capped) | ~$0.21 |

Safety caps in `ClaudeProvider`: 80k input tokens, 4.5k output tokens max.  
Per-call cost logged to backend logs at INFO level.

---

## Session log

| Date | Session | Videos processed | Approx. cost | Notes |
|---|---|---|---|---|
| 2026-05-12 | Session 1 (docs scaffold) | 0 | $0.00 | No pipeline runs |

---

## Template for future sessions

```
| YYYY-MM-DD | Session N | N videos | $X.XX | notes |
```

---

## Scripts

- `scripts/check_api_cost.py` — parses backend logs and sums Claude token usage

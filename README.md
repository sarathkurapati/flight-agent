# Autonomous Browser Agent

An AI agent that accepts a natural-language goal and operates a real browser to complete it — no human in the loop. It takes screenshots, sends them to Claude, and executes whatever action Claude decides until the goal is done or impossible.

## How it works

```
screenshot → Claude vision (ReAct) → JSON action → Playwright executes → repeat
```

Each step:
1. Takes a screenshot of the live browser page
2. Sends it to `claude-opus-4-8` along with the goal and recent history
3. Claude returns one JSON action (`navigate`, `click`, `type`, `scroll`, `wait`, `done`, or `fail`)
4. Playwright executes it
5. Repeat until Claude says `done` or `fail`, or the step limit is hit

## Requirements

- Python 3.11+
- [Anaconda](https://www.anaconda.com/) (recommended) or any Python 3.11 environment
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
# 1. Clone / download the project
cd "AI AGENT"

# 2. Install dependencies (use Anaconda Python)
/opt/anaconda3/bin/pip install -e ".[dev]"

# 3. Install the Chromium browser Playwright needs
/opt/anaconda3/bin/playwright install chromium

# 4. Add your API key
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# Basic — runs headless by default
/opt/anaconda3/bin/python main.py "find the cheapest flight from New York to Mumbai next month"

# Show the browser window
/opt/anaconda3/bin/python main.py --visible "search for Python tutorials on YouTube"

# Increase step budget for complex tasks
/opt/anaconda3/bin/python main.py --max-steps 50 "compare iPhone 16 prices on Amazon and eBay"

# Debug logging
/opt/anaconda3/bin/python main.py --log-level DEBUG "open wikipedia and summarise the history of Rome"
```

Exit codes: `0` = goal completed, `1` = failed or max steps exceeded, `130` = interrupted.

## Configuration

All settings can be set via environment variables in `.env` or overridden on the CLI.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Your Anthropic API key |
| `MODEL` | `claude-opus-4-8` | Claude model to use |
| `MAX_STEPS` | `30` | Hard cap on ReAct iterations |
| `HEADLESS` | `true` | Run browser without a window (set `false` to watch locally) |
| `VIEWPORT_WIDTH` | `1280` | Browser viewport width (px) |
| `VIEWPORT_HEIGHT` | `800` | Browser viewport height (px) |
| `SLOW_MO_MS` | `0` | Slow down Playwright actions (useful for debugging) |
| `NAVIGATION_TIMEOUT_MS` | `30000` | Page load timeout |
| `ACTION_DELAY_MS` | `800` | Pause after each browser action |
| `API_RETRY_ATTEMPTS` | `3` | Retries on Anthropic API errors |
| `DATA_DIR` | `/tmp/browser-agent` | Root for screenshots + session logs (set `DATA_DIR=.` locally) |
| `SCREENSHOTS_DIR` | `$DATA_DIR/screenshots` | Override the screenshots path directly |
| `LOGS_DIR` | `$DATA_DIR/logs` | Override the logs path directly |

## Project structure

```
AI AGENT/
├── pyproject.toml          # Package config + all dependencies
├── .env.example            # Copy to .env and fill in your API key
├── .gitignore
├── main.py                 # CLI entry point
├── api.py                  # FastAPI web service (cloud deployment)
├── Dockerfile              # Production image (Chromium + app)
├── docker-compose.yml      # Local Docker testing
├── agent/
│   ├── __init__.py
│   ├── config.py           # Pydantic-settings — validated at startup
│   ├── exceptions.py       # GoalFailedError, MaxStepsExceededError, ActionParseError
│   ├── prompts.py          # System prompt (edit here to tune agent behaviour)
│   ├── browser.py          # Playwright wrapper with anti-detection + screenshot saving
│   └── core.py             # ReAct loop — retries, session state, logging
├── tests/
│   ├── test_config.py
│   ├── test_exceptions.py
│   ├── test_core.py
│   ├── test_browser.py
│   ├── test_api.py         # FastAPI endpoint tests
│   └── test_integration.py # End-to-end session tests
└── /tmp/browser-agent/     # Default runtime data dir (screenshots + session logs)
```

## Outputs

**Screenshots** — every step is saved as `screenshots/<session_id>/step_NNN_HHMMSS.png`. Useful for debugging why the agent made a particular decision.

**Session logs** — each run writes `logs/session_<id>.json` with every step, the URL at each step, the action taken, and the final result.

**Console** — structured log lines via loguru + a rich result panel at the end.

## Running tests

```bash
/opt/anaconda3/bin/pytest
# with coverage
/opt/anaconda3/bin/pytest --cov
```

## Cloud deployment

### Docker

```bash
# Build the image (~1–2 GB — includes Chromium)
docker build -t browser-agent .

# Run with your API key
docker run -e ANTHROPIC_API_KEY=sk-ant-... -p 8000:8000 browser-agent
```

### docker-compose (recommended for local Docker testing)

```bash
ANTHROPIC_API_KEY=sk-ant-... docker compose up
```

The service starts at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

### API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check — returns `{"status":"ok"}` |
| `/run` | POST | Synchronous — blocks until the agent finishes |
| `/jobs` | POST | Async — returns a `job_id` immediately (202) |
| `/jobs/{id}` | GET | Poll status + result |
| `/jobs/{id}` | DELETE | Remove a completed job |

**Request body (both `/run` and `/jobs`):**
```json
{
  "goal": "find the cheapest flight to Mumbai next month",
  "max_steps": 30,
  "headless": true
}
```

**GET /jobs/{id} response:**
```json
{
  "job_id": "abc123def456...",
  "goal": "find cheapest flight...",
  "status": "done",
  "result": "Found cheapest flight: ...",
  "created_at": "2026-06-17T10:00:00+00:00",
  "updated_at": "2026-06-17T10:04:23+00:00"
}
```

### Cloud-specific environment variables

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/tmp/browser-agent` | Root directory for screenshots, session logs, and the job DB |
| `LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MAX_CONCURRENT_SESSIONS` | `2` | Parallel browser sessions (~500 MB RAM each) |
| `API_KEY` | *(unset)* | Require `X-API-Key` header on all agent endpoints — leave unset to disable auth |
| `MAX_QUEUED_JOBS` | `100` | Max pending+running jobs before `POST /jobs` returns 429 |
| `RUN_TIMEOUT_SECONDS` | `660` | Wall-clock cap for `POST /run` before returning 408 |

### Cloud platform notes

| Platform | Guidance |
|---|---|
| **AWS ECS / Fargate** | Minimum 2 GB memory. Set `--cpu 1024 --memory 2048`. |
| **Google Cloud Run** | `--memory 2Gi --concurrency 2 --no-cpu-throttling` |
| **Fly.io** | `fly deploy` — use `performance-1x` (1 CPU / 2 GB) at minimum. |
| **Render** | "Docker" environment, Standard instance (2 GB RAM). |

> **Note:** Jobs are persisted in a SQLite database (`$DATA_DIR/jobs.db`) and survive container restarts. SQLite does not work across multiple replicas — for multi-instance production use, replace the SQLite helpers in `api.py` with a shared Redis or Postgres store.

## Limitations

- Works best on pages that don't require solving CAPTCHAs or SMS verification
- Coordinate-based clicking means performance varies with page layout changes
- Each step makes one Claude API call — complex tasks with many steps cost accordingly
- The agent cannot handle file downloads or browser popups that steal focus

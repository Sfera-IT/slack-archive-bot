# Slack Archive Bot - Copilot Instructions

## Repository Overview

**Purpose:** A Slack bot that archives and searches Slack message history beyond the 10,000 message limit. The bot uses SQLite for storage, supports semantic search with sentence transformers, and can clean/normalize URLs in archived messages.

**Size:** ~3,500 lines of Python code across 8 main files  
**Language:** Python 3.11+ (Docker uses 3.11, local development works with 3.12)  
**Framework:** Slack Bolt SDK, Flask for WSGI deployment  
**Database:** SQLite with sentence embeddings for semantic search  
**Deployment:** Docker, Gunicorn (WSGI), or development server

## Project Structure

### Root Files
- `archivebot.py` (1,235 lines) - Main bot application with Slack event handlers, message archiving, and search logic
- `flask_app.py` (1,194 lines) - Flask wrapper for WSGI deployment with web UI for browsing/exporting messages
- `utils.py` (348 lines) - Database connection and migration utilities
- `url_cleaner.py` (301 lines) - URL cleaning/normalization to remove tracking parameters
- `url_rules.json` (large JSON) - Comprehensive URL cleaning rules for various domains
- `gunicorn_conf.py` (11 lines) - Gunicorn WSGI configuration
- `requirements.txt` - Python dependencies (Flask, slack-bolt, sentence-transformers, torch, openai, pytest)
- `Dockerfile` - Multi-stage Docker build

### Directories
- `.github/workflows/` - GitHub Actions CI/CD pipeline
- `tests/` - Test suite (pytest)
  - `test_url_cleaner.py` - 30 tests for URL cleaning functionality
- `utilities/` - Helper scripts
  - `import.py` - Import Slack export archives into SQLite
  - `export.py` - Export messages from SQLite to Slack-compatible JSON format
  - `update_embeddings.py` - Batch update sentence embeddings for semantic search
  - `test_embeddings.py` - Test embedding generation
  - `clone_db.sh` - Database cloning utility

### Configuration Files
- `renovate.json` - Renovate bot config (security updates only)
- `.dockerignore` - Excludes .git/, .idea/, *.sqlite
- `.gitignore` - Excludes export/, **.sqlite, **.pyc, IDE files

## Build & Development

### Dependencies Installation

**CRITICAL:** Always install with the CPU-only PyTorch index to avoid large CUDA downloads:

```bash
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

This command takes ~2 minutes due to torch and sentence-transformers. The `--extra-index-url` flag is **required** to use CPU-only PyTorch binaries and is specified in the Dockerfile.

**Python Version:** Requires Python 3.11+ (Docker uses 3.11, tested with 3.12 locally)

### Running Tests

```bash
pytest tests/ -v
```

- All 30 tests in `test_url_cleaner.py` should pass
- Tests complete in ~1-2 seconds
- No special setup required, tests use minimal in-memory rules

### Running the Bot

**Development Server (not for production):**
```bash
SLACK_BOT_TOKEN=<token> SLACK_SIGNING_SECRET=<secret> python archivebot.py
```

Options:
- `-d/--database-path` - SQLite database path (default: ./slack.sqlite)
- `-l/--log-level` - CRITICAL, ERROR, WARNING, INFO, DEBUG (default: DEBUG)
- `-p/--port` - Server port (default: 3333)

**Production WSGI (recommended):**
```bash
SLACK_BOT_TOKEN=<token> SLACK_SIGNING_SECRET=<secret> \
  gunicorn flask_app:flask_app -c gunicorn_conf.py
```

Environment variables:
- `SLACK_BOT_TOKEN` - Required, bot OAuth token
- `SLACK_SIGNING_SECRET` - Required, from Slack app settings
- `ARCHIVE_BOT_DATABASE_PATH` - Optional, database path (default: ./slack.sqlite)
- `ARCHIVE_BOT_LOG_LEVEL` - Optional, log level (default: DEBUG)
- `ARCHIVE_BOT_PORT` - Optional, port (default: 3333)
- `WORKERS` - Optional, Gunicorn workers (default: 4)

### Docker Build & Run

**Build:**
```bash
docker build --build-arg PY_BUILD_VERS=3.11 . -t archivebot:latest
```

Build uses multi-stage Dockerfile:
1. Stage 1: Install build deps (gcc, cmake, etc.) and Python packages
2. Stage 2: Copy to slim image with only ffmpeg runtime dependency

**Run:**
```bash
docker run -e SLACK_BOT_TOKEN=<token> -e SLACK_SIGNING_SECRET=<secret> \
  -v /local/path:/data/ archivebot:latest
```

Database is stored in `/data/slack.sqlite` inside container.

### Utilities

**Import Slack export:**
```bash
# Must run from project root with PYTHONPATH set
PYTHONPATH=/path/to/slack-archive-bot python utilities/import.py <export_directory>
```

**Export to Slack format:**
```bash
python utilities/export.py -d slack.sqlite -o output_directory
```

**Update embeddings:**
```bash
PYTHONPATH=/path/to/slack-archive-bot python utilities/update_embeddings.py -d slack.sqlite
```

**IMPORTANT:** Utility scripts in `utilities/` import `utils` module, so they must be run either:
- As modules: `python -m utilities.import <args>`
- With PYTHONPATH: `PYTHONPATH=/path/to/repo python utilities/import.py <args>`
- From project root with utilities adding ROOT_DIR to sys.path (test files do this)

## CI/CD Pipeline

### GitHub Actions Workflow

File: `.github/workflows/docker-publish.yml`

**Triggers:**
- Daily cron: `45 15 * * *`
- Push to any branch
- Tags: `v*.*.*`
- Pull requests to master

**What it does:**
1. Checks out repository
2. Sets up Docker buildx
3. Logs into GitHub Container Registry (ghcr.io) - only for non-PR events
4. Builds Docker image with `PY_BUILD_VERS=3.11`
5. Pushes to ghcr.io (skipped for PRs)

**No traditional test step** - the workflow only builds and publishes Docker images. Run `pytest tests/` locally to validate changes.

## Key Architecture Details

### Database Schema

Four main tables (see `utils.py` for migration logic):
- `messages` - message text, user, channel, timestamp, permalink (unique on channel+timestamp)
- `users` - name, id, avatar, deleted flag, real_name, display_name, email
- `channels` - name, id, is_private flag
- `members` - channel-user relationships

The database also stores:
- Message embeddings (sentence-transformers) for semantic search
- User clown list (for a custom feature)
- AI conversation history (for OpenAI integration in flask_app)

### URL Cleaning

`url_cleaner.py` implements ClearURLs-like functionality:
- Provider-specific rules via regex patterns
- Redirect extraction (Google, Facebook, etc.)
- Tracking parameter removal (utm_*, fbclid, etc.)
- Falls back to removing all query params for unknown domains
- Loads rules from `url_rules.json` (comprehensive) or uses minimal default rules

Tests verify 30+ scenarios including Google redirects, YouTube, Amazon, social media, affiliate links.

### Main Bot Logic (`archivebot.py`)

- Event handlers for Slack events (messages, channel changes, user updates)
- Search functionality via DM: `<query> from:<user> in:<channel> sort:asc|desc limit:<number>`
- Supports both keyword search (SQL LIKE) and semantic search (sentence embeddings)
- OpenAI integration for summarization (requires OPENAI_API_KEY)
- Lazy-loads SentenceTransformer model on first use (paraphrase-MiniLM-L6-v2)
- URL cleaning applied to all archived messages

### Flask Web UI (`flask_app.py`)

- `/slack/events` - Slack Events API endpoint (handled by SlackRequestHandler)
- Web interface for browsing messages with authentication
- Export functionality (CSV, JSON)
- Admin-only features (hardcoded admin user IDs at top of file)
- Audio transcription (uses OpenAI Whisper API)
- Requires JWT for auth, uses Slack OAuth

## Common Pitfalls & Workarounds

### 1. Utility Scripts Import Errors

**Problem:** Running `python utilities/import.py` fails with `ModuleNotFoundError: No module named 'utils'`

**Solution:** Utilities import from project root. Either:
- Use module syntax: `python -m utilities.import <args>`
- Set PYTHONPATH: `PYTHONPATH=/path/to/slack-archive-bot python utilities/import.py`
- Tests handle this in test files by adding ROOT_DIR to sys.path (see test_url_cleaner.py lines 5-8)

### 2. PyTorch Size

**Problem:** Default pip install downloads 2GB+ CUDA version of PyTorch

**Solution:** Always use `--extra-index-url https://download.pytorch.org/whl/cpu` (already in Dockerfile)

### 3. Gunicorn Worker Database Init

**Problem:** Multiple Gunicorn workers can cause race conditions during database initialization

**Solution:** `gunicorn_conf.py` uses `on_starting` hook to run `init()` once before workers spawn. Don't call `init()` in worker processes.

### 4. Missing Environment Variables

**Problem:** Bot fails to start without SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET

**Solution:** These are required environment variables. Get them from Slack app settings:
- Bot token: OAuth & Permissions page
- Signing secret: Basic Information page

## Testing Changes

1. **Install dependencies** (if not already done):
   ```bash
   pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
   ```

2. **Run tests:**
   ```bash
   pytest tests/ -v
   ```

3. **For code changes to archivebot.py or flask_app.py:**
   - Create test Slack app credentials (for integration testing)
   - Or verify logic changes don't break existing tests
   - Test URL cleaning if modifying url_cleaner.py (30 tests should pass)

4. **For Docker changes:**
   ```bash
   docker build --build-arg PY_BUILD_VERS=3.11 . -t archivebot:test
   docker run -e SLACK_BOT_TOKEN=test -e SLACK_SIGNING_SECRET=test archivebot:test
   ```

5. **For dependency updates:**
   - Update requirements.txt
   - Test with `pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu`
   - Run all tests
   - Verify Docker build succeeds

## Key Files to Know

When making changes, these are the most commonly modified files:

1. **archivebot.py** - For bot behavior, event handlers, search logic
2. **flask_app.py** - For web UI, exports, admin features
3. **url_cleaner.py** - For URL normalization logic
4. **url_rules.json** - For URL cleaning rule updates (new domains, tracking params)
5. **requirements.txt** - For dependency updates
6. **Dockerfile** - For Docker build changes
7. **tests/test_url_cleaner.py** - For URL cleaning test coverage

## Summary

This is a Python-based Slack bot with these characteristics:

- **No linting configured** - No flake8, pylint, black, or other code quality tools in repo
- **Simple test suite** - Only URL cleaner has tests; pytest is in requirements.txt
- **Docker-first deployment** - Primary deployment via multi-stage Dockerfile + Gunicorn
- **Environment-driven config** - All settings via env vars or CLI args
- **Minimal dependencies** - Main deps: slack-bolt, Flask, sentence-transformers, torch, openai
- **SQLite storage** - Single-file database with embeddings

**Trust these instructions.** Only search for additional information if:
- The instructions are incomplete for your specific task
- You encounter errors not documented here
- You need to understand implementation details not covered

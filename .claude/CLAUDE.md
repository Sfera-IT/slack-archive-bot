# Slack Archive Bot - Claude Context

## Project Overview

**Slack Archive Bot** is a Python bot that archives Slack messages beyond the 10,000 message limit, enabling full-text search and AI-powered analysis of historical conversations.

### Core Capabilities
- **Message Archiving**: Captures all messages from public/private channels and DMs
- **Historical Search**: Full-text search across the complete archive
- **AI Features**: Thread analysis with GPT-4o, daily digests, podcast generation, semantic search
- **Link Management**: Duplicate detection (45 days), tracking parameter removal, xcancel.com alternatives
- **Privacy Controls**: User opt-out, AI opt-out, admin controls

---

## Architecture

```
┌─────────────────────────────────┐
│     Slack Workspace             │
└──────────────┬──────────────────┘
               │ Events (Slack Bolt)
               ↓
┌─────────────────────────────────┐
│   archivebot.py (1304 lines)    │
│   - Event handlers              │
│   - Message processing          │
│   - Link detection              │
│   - AI features (GPT-4o)        │
└──────────────┬──────────────────┘
               │
        ┌──────┴──────┐
        ↓             ↓
┌──────────────┐ ┌──────────────────┐
│ slack.sqlite │ │ url_rules.json   │
│   (~1GB)     │ │ (ClearURLs rules)│
└──────────────┘ └──────────────────┘

┌─────────────────────────────────┐
│   flask_app.py (1194 lines)     │
│   REST API + OAuth              │
└─────────────────────────────────┘
```

### Key Files

| File | Purpose |
|------|---------|
| `archivebot.py` | Main bot logic, Slack event handlers, AI features |
| `flask_app.py` | REST API server, OAuth, search endpoints |
| `utils.py` | Database utilities, migrations, helpers |
| `url_cleaner.py` | URL normalization, tracking parameter removal |
| `url_rules.json` | ClearURLs-style rules (250+ providers) |
| `gunicorn_conf.py` | Production server configuration |

### Utilities

| File | Purpose |
|------|---------|
| `utilities/import.py` | Import historical Slack exports |
| `utilities/export.py` | Export archive to JSON |
| `utilities/update_embeddings.py` | Generate semantic embeddings |

---

## Tech Stack

```
Python 3.12
├── Flask 3.1.1           # REST API
├── Slack Bolt 1.18.0     # Official Slack framework
├── Gunicorn 23.0.0       # Production WSGI
├── OpenAI 1.47.0         # GPT-4o integration
├── SentenceTransformers  # Semantic search (paraphrase-MiniLM-L6-v2)
├── PyTorch 2.8.0         # ML backend (CPU only)
├── Pydub 0.25.1          # Audio processing
├── SQLite3               # Database
└── pytest 8.3.3          # Testing
```

---

## Database Schema

### Core Tables

```sql
-- Archived messages
messages (
  message TEXT,
  user TEXT,
  channel TEXT,
  timestamp TEXT,       -- Unique with channel
  permalink TEXT,
  thread_ts TEXT,
  embeddings BLOB       -- 384-dim float vectors
)

-- Users
users (id, name, avatar, is_deleted, real_name, display_name, email)

-- Channels
channels (id, name, is_private)

-- Channel membership
members (channel FK, user FK)
```

### Feature Tables

```sql
-- Link tracking (45-day window)
posted_links (normalized_url, original_url, message_timestamp,
              channel, permalink, posted_date, duplicate_notified)

-- Duplicate alerts (for cleanup on parent deletion)
duplicate_alerts (parent_message_ts, alert_message_ts, channel)

-- User opt-outs
optout (user FK, timestamp)      -- Full archive opt-out
optout_ai (user FK)              -- AI features opt-out

-- Rate limiting
ai_requests (timestamp, user_id, channel)  -- 2/min, 10/hour

-- Temporary "clown" reactions
clown_users (nickname UNIQUE, expiry_date)

-- Generated digests
digests (timestamp, period, digest, posts, podcast_content)
```

---

## Configuration

### Required Environment Variables

```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...

# Flask
SECRET_KEY=...              # JWT signing

# OAuth (for web UI)
CLIENT_ID=...
CLIENT_SECRET=...
OAUTH_SCOPE=users.profile:read
EXPECTED_TEAM_ID=T...
CLIENT_URL=http://localhost:3000

# AI Features
OPENAI_API_KEY=sk-...

# Database
ARCHIVE_BOT_DATABASE_PATH=/data/slack.sqlite
```

### Optional Configuration

```bash
ARCHIVE_BOT_PORT=3333                    # Default port
ARCHIVE_BOT_LOG_LEVEL=INFO               # DEBUG|INFO|WARNING|ERROR|CRITICAL
WORKERS=4                                # Gunicorn workers
```

---

## Bot Commands

### DM Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/clown <nickname>` | Add user to clown list (7 days) | Any user |
| `/clownremove <nickname>` | Remove user from clown list | Any user |
| `/optout <@user_id>` | Anonymize all user messages | **Admin only** |

### Channel Features

| Feature | Trigger | Description |
|---------|---------|-------------|
| Thread Analysis | `@archivebot` mention | GPT-4o analyzes thread context |
| Duplicate Detection | Automatic | Alerts on duplicate links (45 days) |
| xcancel Links | Automatic | Posts xcancel.com alternative for x.com |

### Rate Limits

- **AI requests**: 2 per minute, 10 per hour per user
- Response includes remaining quota info

---

## API Endpoints (flask_app.py)

### Search
- `GET /api/messages?query=...&from=...&in=...` - Search messages
- `GET /api/channels` - List channels
- `GET /api/users` - List users

### Auth
- `GET /api/oauth/authorize` - Start OAuth flow
- `GET /api/oauth/callback` - OAuth callback
- `POST /api/verify` - Verify JWT token

### Stats
- `GET /api/stats` - Archive statistics

---

## Development

### Local Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run bot
SLACK_BOT_TOKEN=xoxb-... \
SLACK_SIGNING_SECRET=... \
python archivebot.py -p 3333

# Expose via ngrok for Slack events
ngrok http 3333
```

### Testing

```bash
pytest tests/ -v
```

### VSCode Debug

Launch configuration available in `.vscode/launch.json`

---

## Production Deployment

### Docker

```bash
# Build
docker build \
  --build-arg PY_BUILD_VERS=3.12 \
  --build-arg PORT=3333 \
  -t archivebot:latest .

# Run
docker run \
  -p 3333:3333 \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e SLACK_SIGNING_SECRET=... \
  -e OPENAI_API_KEY=sk-... \
  -v /local/data/:/data/ \
  archivebot:latest
```

### Gunicorn (without Docker)

```bash
gunicorn flask_app:flask_app -c gunicorn_conf.py
```

### Requirements
- Python 3.12+
- 2GB+ RAM (ML models)
- 20GB+ storage (database)
- Public URL for Slack webhooks
- ffmpeg (for podcast generation)

---

## Code Patterns

### Event Handling

```python
@app.message("")
def handle_message(message, say, client):
    # Check opt-out status
    # Save to database
    # Process links
    # Check AI mentions
```

### AI Integration

```python
# Thread analysis
response = openai.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "system", "content": SYSTEM_PROMPT}, ...],
    max_tokens=4000
)

# Podcast TTS
audio_response = openai.audio.speech.create(
    model="tts-1",
    voice="alloy",
    input=text
)
```

### URL Cleaning

```python
from url_cleaner import normalize_url
clean_url = normalize_url(raw_url, rules)  # Uses url_rules.json
```

### Embeddings

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
embeddings = model.encode(text)  # Returns 384-dim vector
```

---

## Important Implementation Details

### Message Deletion Handling
- Handles both `message_deleted` and `message_changed` events
- Auto-deletes duplicate alerts when parent message is deleted
- Updates `duplicate_alerts` table for cleanup tracking

### Privacy Implementation
- Opt-out replaces message text with "User opted out of archiving"
- AI opt-out prevents GPT analysis of user's content
- Admin `/optout` command retroactively anonymizes all messages

### Link Duplicate Detection
- 45-day rolling window
- URL normalization removes tracking params before comparison
- Posts notification in thread, not channel

### Clown Feature
- Adds emoji reaction to all messages from targeted user
- Auto-expires after 7 days
- Stored by nickname (display_name/real_name match)

---

## Logging

Log levels controlled by `ARCHIVE_BOT_LOG_LEVEL`:

| Tag | Purpose |
|-----|---------|
| `[AI]` | Bot mention processing |
| `[DUPLICATE_LINK_DETECTED]` | Link duplicate found |
| `[MESSAGE_DELETED]` | Message deletion events |
| `[CLOWN]` | Clown user tracking |
| `[OPTOUT]` | Opt-out execution |

---

## External Integrations

### Slack API Scopes

```
channels:history, channels:join, channels:read, chat:write
groups:history, groups:read, im:history, users:read
```

### Slack Events Subscribed

```
message.channels, message.groups, message.im
channel_created, channel_rename, group_rename
member_joined_channel, member_left_channel
user_change, message_deleted, message_changed
app_mention, thread_broadcast
```

---

## Common Tasks

### Import Historical Data

```bash
python utilities/import.py /path/to/slack-export/
```

### Export Archive

```bash
python utilities/export.py --output archive.json
```

### Update Embeddings

```bash
python utilities/update_embeddings.py
```

### Database Migrations

Migrations run automatically on startup via `gunicorn_conf.py` or `utils.init()`.

---

## Troubleshooting

### Bot not receiving events
1. Check ngrok/public URL is accessible
2. Verify `SLACK_SIGNING_SECRET` matches app settings
3. Confirm event subscriptions in Slack app dashboard

### AI features not working
1. Verify `OPENAI_API_KEY` is set
2. Check rate limit (2/min, 10/hour)
3. Review logs for `[AI]` tagged entries

### Missing messages in archive
1. Check user hasn't opted out
2. Verify bot is member of channel
3. Review channel permissions (private vs public)

### Embeddings failing
1. Ensure sufficient RAM (2GB+)
2. Check PyTorch installation
3. Run `utilities/test_embeddings.py`

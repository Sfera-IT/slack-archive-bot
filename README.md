# slack-archive-bot

A bot that can search your slack message history.  Makes it possible to search
further back than 10,000 messages.

## Requirements

1. Permission to install new apps to your Slack workspace.
2. Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) can install and manage it.
3. A publicly accessible URL to serve the bot from. (Slack recommends using [ngrok](https://ngrok.com/) to get around this.)

## Installation

1. Clone this repo.
2. Install the requirements:

        uv sync

3. If you want to include your existing slack messages, [export your team's slack history.](https://get.slack.help/hc/en-us/articles/201658943-Export-your-team-s-Slack-history)
Download the archive and export it to a directory. Then run `import.py`
on the directory.  For example:

        uv run python utilities/import.py export

    This will create a file `slack.sqlite`.
    
4. Create a new [Slack app](https://api.slack.com/start/overview).

- Add the following bot token oauth scopes and install it to your workspace:

  - `channels:history`
  - `channels:join`
  - `channels:read`
  - `chat:write`
  - `groups:history` (if you want to archive/search private channels)
  - `groups:read` (if you want to archive/search private channels)
  - `im:history`
  - `users:read`

5. Start slack-archive-bot with:

        SLACK_BOT_TOKEN=<BOT_TOKEN> SLACK_SIGNING_SECRET=<SIGNING_SECRET> uv run python archivebot.py

Where `SIGNING_SECRET` is the "Signing Secret" from your app's "Basic Information" page and `BOT_TOKEN` is the
"Bot User OAuth Access Token" from the app's "OAuth & Permissions" page.

Use `uv run python archivebot.py -h` for a list of all command line options.

6. Go to the app's "Event Subscriptions" page and add the url to where slack-archive-bot is being served. The default port is `3333`. (i.e. `http://<ip>:3333/slack/events`)

- Then add the following bot events:

  - `channel_created`
  - `channel_rename`
  - `group_rename` (if you want to archive/search private channels)
  - `member_joined_channel`
  - `member_left_channel`
  - `message.channels`
  - `message.groups` (if you want to archive/search private channels)
  - `message.im`
  - `user_change`

## Run with docker

Build the latest docker image with:

```shell
docker build --build-arg PORT=3333 . -t archivebot:latest
```

Run the built image using:

```shell
docker run -e SLACK_BOT_TOKEN=<BOT_TOKEN> -e SLACK_SIGNING_SECRET=<SIGNING_SECRET> -v /local/data/path/:/data/ archivebot:latest
```

## Deploying Production Server Using WSGI

By default when you run `uv run python archivebot.py` it will launch a development server. But they don't recommend using it in production. The following is an example of using
Flask and Gunicorn to deploy slack-archive-bot, but it should work equally well with any other WSGI server. 

1. `SLACK_BOT_TOKEN=<BOT_TOKEN> SLACK_SIGNING_SECRET=<SIGNING_SECRET> uv run gunicorn flask_app:flask_app -c gunicorn_conf.py <other gunicorn args>`
2. `flask_app.py` provides a thin wrapper around `archivebot.app` using `slack_bolt.adapter.flask.SlackRequestHandler`. There are many other adapters provided by bolt. To use them, simply `from archivebot import app` and wrap `app`.
3. `gunicorn_conf.py` ensures that the local database is updated when the server is started, but that it's not run for each worker.
4. You can use `ARCHIVE_BOT_LOG_LEVEL` and `ARCHIVE_BOT_DATABASE_PATH` to configure slack-archive-bot while running it via gunicorn. 

## Archiving New Messages

When running, ArchiveBot will continue to archive new messages for any channel it
is invited to.  To add the bot to your channels:

        /invite @ArchiveBot

If @ArchiveBot is the name you gave your bot user.

## Duplicate link and story detection

Every external HTTP(S) link shared in a channel root or thread reply is checked
against the preceding 45 days. Slack links and messages without external links
are ignored.

- The same normalized URL produces an immediate *same link* alert.
- A different URL with the same extracted article text produces a *same content*
  alert after background enrichment.
- A different URL whose extracted content exceeds the configured similarity
  threshold produces a clearly labelled *potentially the same story* alert.

Alerts cite the earlier Slack permalink and appear in the new message's thread.
A repost inside the same Slack thread is silent. Cross-channel same-content and
same-story comparisons run only between public channels; private-channel content
is never surfaced elsewhere. If Slack delivery is ambiguous, the bot suppresses
automatic retry rather than risk posting the warning twice.

Link enrichment is asynchronous and durable in SQLite. Development mode starts
one daemon worker; Gunicorn starts one atomically coordinated worker per process.
Same-story comparison also uses leased SQLite scan state: each worker iteration
examines a bounded batch and resumes later without marking a link checked until
its complete 45-day candidate space has been evaluated.
The fetcher accepts public HTML only, pins connections to validated public IPs,
revalidates redirects, and applies one DNS-through-body deadline plus response
size and connection limits. It does not execute JavaScript, authenticate to
sites, bypass paywalls, or extract PDFs/media. Metadata-only pages require a
higher similarity score; failed or unsupported pages retain exact-link checking
but cannot produce a semantic match.

Configuration:

- `LINK_ENRICHMENT_ENABLED` — enable the worker; default `true`.
- `LINK_TOPIC_SIMILARITY_THRESHOLD` — semantic threshold from `0` to `1`;
  default `0.92`. Metadata-only comparisons require at least `0.97`.
- `LINK_FETCH_TOTAL_TIMEOUT_SECONDS` — total DNS-through-body deadline; default
  `15`.
- `LINK_FETCH_CONNECT_TIMEOUT_SECONDS`, `LINK_FETCH_READ_TIMEOUT_SECONDS`, and
  `LINK_FETCH_POOL_TIMEOUT_SECONDS` — per-operation ceilings inside the total
  deadline; defaults `3`, `5`, and `2`.
- `LINK_FETCH_MAX_BYTES` — decompressed HTML cap; default `3145728`.
- `LINK_FETCH_MAX_REDIRECTS` — redirect cap; default `5`.
- `LINK_FETCH_MAX_CONNECTIONS` — per-worker connection cap; default `4`.
- `LINK_FETCH_CACHE_TTL_SECONDS` — successful document cache lifetime; default
  `604800`.
- `LINK_ENRICHMENT_POLL_SECONDS` and `LINK_ENRICHMENT_ERROR_BACKOFF_SECONDS` —
  idle polling and transient-error backoff; defaults `2` and `5`.

## Searching

To search the archive, direct message (DM) @ArchiveBot with the search query.
For example, sending the word "pizza" will return the first 10 messages that
contain the word "pizza".  There are a number of parameters that can be provided
to the query.  The full usage is:

        <query> from:<user> in:<channel> sort:asc|desc limit:<number>

        query: The text to search for.
        user: If you want to limit the search to one user, the username.
        channel: If you want to limit the search to one channel, the channel name.
        sort: Either asc if you want to search starting with the oldest messages,
            or desc if you want to start from the newest. Default asc.
        limit: The number of responses to return. Default 10.

## AI thread engagement

Mentioning the bot normally keeps the default one-shot behavior: it replies once
using the current thread or channel context. Historical questions now run a
bounded agentic search over the complete archived history visible to the
requesting user. Questions with explicit historical intent force an initial
archive grep, so the model cannot answer them from plausible-sounding memory.
The agent can then iteratively:

- grep messages and metadata across all visible channels;
- refine searches with names, synonyms, dates, and channel filters;
- sort by relevance, newest, or oldest results;
- open a matching thread or inspect surrounding messages;
- cite archived Slack messages with their permalinks.

The retrieval path deliberately does not depend on the legacy message embeddings.
Messages removed from the archive or excluded through archive/AI opt-out are never
returned. Public channels are searchable workspace-wide; private-channel results
are available only to members (the current private channel is also allowed because
the request itself proves access).

AI answers use `gpt-5.6-sol` and the OpenAI Responses API by default. The bounded
agent preserves every response output item between stateless tool turns, including
reasoning items, and returns local archive results as `function_call_output` items.
Configure it with:

- `OPENAI_MODEL` — answer and retrieval model; default `gpt-5.6-sol`.
- `OPENAI_REASONING_EFFORT` — `none`, `low`, `medium`, `high`, `xhigh`, or `max`;
  default `medium`.
- `OPENAI_DECISION_MODEL` — optional override for legacy engage/clown decisions;
  defaults to `OPENAI_MODEL`.

The Responses API allows GPT-5.6 reasoning and archive function tools in the same
request. The configured reasoning effort therefore applies to the complete search
loop; the default is `medium`.

The response policy is evidence-first and intentionally sober: no automatic
sarcasm, recurring inside jokes, or claims of remembering a conversation that was
not found. When the archive does not provide enough evidence, the bot says so.
Current channel/thread context is size-bounded and keeps the most recent messages,
so a long conversation cannot crowd retrieval evidence out of the model window.

To keep the bot engaged in a thread, mention it with:

        @ArchiveBot /engage

From that point on, the bot replies to every new user message in that thread,
on any channel where the app receives message events. Engage events are claimed
atomically to avoid duplicate Slack deliveries. If live thread history cannot be
read, the bot falls back to the locally archived thread. To stop it, use the
`Zitto` button or mention the bot with `stop`, for example:

        @ArchiveBot stop

Sending `@ArchiveBot /engage` again in the same thread reactivates it.

Successful AI replies include the current per-user quota (`2/minute`, `10/hour`).
Engaged replies use the same quota and show a clear retry time when it is reached.

### Private AI diagnostics

AI diagnostics are disabled by default. An administrator can enable sanitized
private error reports by sending the bot a DM containing:

        debug on

The reliable DM forms are `debug`, `debug on`, `debug off`, and `debug status`.
They do not require Slack Slash Command configuration. If `/debug` is registered
in the Slack app, the same arguments are also handled as a native command.
Reports include a correlation ID,
the failing flow (including engaged threads), API metadata, and a bounded stack
without prompt contents, local variables, tokens, or credentials. Public channels
only receive the correlation ID.


## Migrating from slack-archive-bot v0.1

`slack-archive-bot` v0.1 used the legacy Slack API which Slack [ended support for in February 2021](https://api.slack.com/changelog/2020-01-deprecating-antecedents-to-the-conversations-api). To migrate to the new version:

- Follow the installation steps above to create a new slack app with all of the required permissions and event subscriptions.
- The biggest change in requirements with the new version is the move from the [Real Time Messaging API](https://api.slack.com/rtm) to the [Events API](https://api.slack.com/apis/connections/events-api) which necessitates having a publicly-accessible url that Slack can send events to. If you are unable to serve a public endpoint, you can use [ngrok](https://ngrok.com/).

## Contributing

Contributions are more than welcome.  From bugs to new features. I threw this
together to meet my team's needs, but there's plenty I've overlooked.

## License

Code released under the [MIT license](LICENSE).

# Goal: Link-Only Duplicate And Same-Story Detection

Work in `/Users/smarzola/projects/slack-archive-bot`.

Detect duplicate external links and high-confidence same-story links in every
link-bearing Slack message, including thread replies, without delaying Slack
event handling or leaking private-channel context. Preserve exact normalized-URL
matching as the deterministic fast path and enrich previously unseen URLs in a
bounded background pipeline before semantic comparison.

Source of truth: the product decisions in the current Codex thread and this
prompt.

## Target State

When this goal is complete:

- Every message containing an external HTTP(S) link, whether a channel root or
  thread reply, enters duplicate processing; messages without links do not.
- Exact normalized-URL matches remain deterministic and are reported without
  waiting for enrichment.
- Previously unseen links are queued, safely fetched, cached, and represented by
  canonical metadata, bounded main text, a content hash, and an embedding.
- Different URLs posted in different public channels during the preceding 45
  days can produce one clearly labelled potential-same-story alert when evidence
  exceeds a deliberately high threshold. Identical extracted content is
  distinguished from semantic similarity.
- Alerts appear in the new message's thread, are idempotent per posted message,
  cite the earlier Slack permalink, and never surface private-channel content
  cross-channel. Reposts within the same thread do not alert.
- Edits and deletions invalidate queued/enriched message-link state and remove
  obsolete alerts without deleting shared cached documents still referenced by
  other messages.

## Current-State Evidence

Verified at base commit `b436c01588c1fe757695501a82a8d0ed9fb5b47f` on branch
`feat/link-topic-duplicates`, created from refreshed `origin/master`:

- `archivebot.py:check_and_store_links` normalizes external links, searches
  `posted_links` over 45 days, and posts exact-link alerts, but explicitly skips
  thread replies and suppresses later alerts by marking the earlier link row.
- `archivebot.py:handle_message` archives a per-message embedding and calls link
  checking after committing the Slack message.
- `archivebot.py:handle_message_changed` updates message text but does not
  synchronize general link duplicate state; deletion removes `posted_links` and
  the legacy duplicate alert.
- `utils.py:migrate_db` owns additive SQLite migrations and already provides
  indexes and atomic claim/finalize helpers for concurrent Slack events.
- `pyproject.toml` already includes HTTPX and requires dependency changes through
  `uv`; no robust HTML main-content extractor is present.
- Baseline verification passed on 2026-07-12:
  `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv lock --check`
  and `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests`
  (`53 passed`).

Unknowns that may affect implementation details, but not the target state:

- Production URL/domain distribution and the similarity-score distribution are
  unavailable locally. Use a conservative configurable threshold and explicit
  potential-match wording; do not claim production precision from fixtures.

## Constraints And Non-Goals

Follow `AGENTS.md`; use `uv`, edit `pyproject.toml`, and refresh `uv.lock` for
dependency changes.

- Only messages containing non-Slack HTTP(S) links are in scope. Do not add
  general conversation/topic duplicate detection.
- Preserve URL cleaning and the existing 45-day window unless a compatibility
  defect requires an explicitly recorded adjustment.
- Do not fetch in the Slack event-response path. Queue durable work and allow
  concurrent workers to claim it atomically.
- Fetch only public HTTP(S) destinations. Reject credentials, private, loopback,
  link-local, multicast, reserved, and metadata-service destinations for IPv4
  and IPv6; validate every redirect hop; cap redirects, time, response bytes,
  accepted content types, retries, and worker concurrency. Send no Slack secrets,
  cookies, authorization, or referer headers.
- Prefer metadata-only degradation over browser automation, authenticated fetch,
  JavaScript execution, PDF/media extraction, or domain-specific adapters. Those
  are non-goals.
- Exact duplicates may be reported in the same channel or across public
  channels. A cross-channel source is eligible only when both channels are
  public. Potential same-story matches require distinct public channels.
- Never alert for two links in the same Slack thread. Emit no more than one
  duplicate alert for a newly posted message, choosing deterministic evidence
  before semantic evidence.
- Keep cached documents separate from message-link and alert lifecycle state.
- Preserve unrelated user changes and backward-compatible startup against an
  existing SQLite database.

## Authorization And Decisions

This goal authorizes repository inspection, in-scope local edits, branch-local
Conventional Commit checkpoints, dependency locking, and relevant non-destructive
verification. It does not authorize pushing, opening or merging a pull request,
publishing, releasing, destructive actions, or secrets/permission changes.

Continue through routine implementation choices using repository evidence. Ask
only when ambiguity would materially change user-visible behavior, architecture,
data compatibility, security posture, or authorization. Before declaring a
blocker, exhaust safe in-scope alternatives and record the evidence.

Material decisions:

- The primary commit type is `feat`.
- Link enrichment is asynchronous and durable rather than performed inline.
- Exact normalized URL, identical extracted content, and semantic same-story are
  separate match classes in descending confidence order.
- Semantic similarity uses the repository's existing sentence-transformer model;
  no LLM verifier or new external AI call is required for the initial feature.
- Production defaults must be configurable by environment variables and safe
  when enrichment is temporarily unavailable.
- `timedelta==2020.12.3` was removed from project dependencies during Milestone
  1: refreshed `uv lock` could no longer resolve that third-party package, and
  repository code imports only the standard-library `datetime.timedelta`.
- `charset-normalizer` was advanced from `3.3.2` to `3.4.4`, the minimum
  compatible pinned line available for Trafilatura 2.1's `>=3.4.0` requirement.
- Milestone 1 review required actual-IP connection pinning (with original Host
  and TLS SNI), a total wall-clock fetch deadline, and token-owned job leases
  with bounded abandoned-claim recovery. These are security/concurrency
  invariants rather than deployment assumptions.
- The same fetch deadline covers DNS through bounded dnspython A/AAAA lookups;
  OS `getaddrinfo` was rejected for production because it cannot be cancelled
  reliably within the worker's total budget.

## Success Criteria

The goal is complete only when:

1. Link extraction, exclusion, normalization, and deterministic duplicate checks
   cover roots and replies, suppress same-thread matches, respect public/private
   boundaries, and remain idempotent under repeated/concurrent Slack events.
2. A durable SQLite-backed enrichment pipeline safely claims and processes new
   URLs outside the event handler, caches successful and failed outcomes, and
   enforces SSRF, redirect, timeout, content-type, and response-size boundaries.
3. Enrichment extracts canonical metadata and bounded main text, hashes normalized
   content, stores a reusable embedding, and degrades explicitly when only
   metadata or no usable content is available.
4. Different URLs can be classified as identical content or potential same story
   within 45 days across distinct public channels, with deterministic evidence
   taking precedence and a conservative configurable semantic threshold.
5. At most one correctly worded, cited alert is posted per new message; alert
   state belongs to the new message/match rather than the historical source, and
   edits/deletions reconcile jobs, message-link rows, and alerts.
6. Automated tests cover migrations, atomic claims, safe fetching and redirects,
   extraction fallbacks, roots/replies, exact/content/semantic classification,
   privacy, same-thread suppression, idempotency, and edit/delete cleanup without
   live Slack, OpenAI, DNS, or arbitrary Internet dependencies.
7. Operator documentation describes the behavior, configuration, limitations,
   and background-worker lifecycle without overstating semantic accuracy.
8. Every milestone is checked off with verification evidence, an adversarial
   milestone review has no blocking findings, and a focused Conventional Commit
   checkpoint exists.
9. Final verification and a fresh independent audit report no blocking findings.

## Milestones

- [x] Milestone 1: Durable safe link enrichment foundation
- [ ] Milestone 2: Deterministic duplicate processing for every link message
- [ ] Milestone 3: Same-content and same-story matching, lifecycle, and operations

### Checkpoint Protocol

At the end of each milestone:

1. Satisfy its acceptance criteria and run its exact verification commands.
2. Freeze writes and pass the diff plus evidence through the retained adversarial
   reviewer; repair and re-review until no blocking finding remains.
3. Mark the checklist item `[x]` and add a dated status note with outcome,
   commands, and results.
4. Commit implementation, tests, documentation, and this prompt update together
   with a focused Conventional Commit message.
5. Report the resulting hash before beginning the next milestone.

Failed verification leaves the milestone unchecked and uncommitted until the
in-scope defect is repaired. A commit cannot contain its own final hash.

## Milestone 1: Durable Safe Link Enrichment Foundation

Why this matters:

- Semantic comparison is trustworthy only after arbitrary URLs can be fetched,
  extracted, cached, and retried without blocking Slack or exposing internal
  services.

Acceptance criteria:

- Additive migrations provide document, message-link, and durable job state with
  appropriate uniqueness, retry, status, timestamp, and lookup indexes.
- The event-facing enqueue operation is local, bounded, and idempotent; an atomic
  worker claim supports multiple Gunicorn workers.
- A testable fetcher validates schemes, host resolution, addresses, ports, and
  every manual redirect, streams within configured limits/timeouts, accepts only
  supported HTML content, and records bounded failure states.
- HTML extraction stores canonical/final URLs, title, description, bounded main
  text, extraction quality, normalized content hash, and embedding input without
  trusting metadata as a fetch destination.
- The worker has explicit development and Gunicorn lifecycle hooks and cannot
  prevent process shutdown.

Likely touchpoints (non-exhaustive):

- `utils.py`
- a focused link-enrichment module
- `archivebot.py`
- `gunicorn_conf.py`
- `pyproject.toml`, `uv.lock`
- focused new tests

Verification:

```bash
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv lock --check
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests/test_link_enrichment.py tests/test_utils.py
```

Status: Complete on 2026-07-12. Milestone-start commit: `31bff42`.

- Outcome: added additive document/message-link/job state, idempotent enqueue,
  token-fenced atomic claims, bounded stale recovery, pinned-IP safe HTTP fetch,
  one DNS-through-body deadline, bounded HTML extraction/cache state, and daemon
  worker lifecycle hooks for development and Gunicorn.
- Verification:
  - `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv lock --check`
    passed with 83 packages.
  - `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests/test_link_enrichment.py tests/test_utils.py`
    passed: 29 tests.
  - `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests`
    passed: 73 tests.
  - `UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run python -m py_compile archivebot.py gunicorn_conf.py link_enrichment.py utils.py`
    passed.
  - `git diff --check` passed.
- Adversarial review: three repair rounds resolved DNS rebinding, true total
  deadlines including DNS, token-owned stale claims, bounded recovery, and
  worker-loop survival; final result `CLEAN`. Residual non-blocking risk: the
  deadline transport relies on pinned HTTPX/httpcore internals and its focused
  tests must accompany future dependency upgrades.

## Milestone 2: Deterministic Duplicate Processing For Every Link Message

Why this matters:

- Exact-link matching is the highest-confidence behavior and must remain fast
  while fixing thread-reply coverage, privacy, idempotency, and alert ownership.

Acceptance criteria:

- Root posts and replies containing external links are recorded and checked;
  link-free and Slack-link-only messages do no enrichment work.
- Exact normalized-URL matches are found within 45 days, except same-thread
  matches; cross-channel matches require both channels to be public.
- Each eligible new message produces at most one exact alert located in its
  current thread and citing the earlier permalink.
- Repeated or concurrent event delivery cannot duplicate message-link rows,
  jobs, or alerts.
- Legacy data remains readable; the new alert model does not depend on mutating
  `duplicate_notified` on the historical source.

Likely touchpoints (non-exhaustive):

- duplicate orchestration module
- `archivebot.py`
- `utils.py`
- focused duplicate behavior tests

Verification:

```bash
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests/test_link_duplicates.py tests/test_utils.py
```

Status: Not started.

## Milestone 3: Same-Content And Same-Story Matching, Lifecycle, And Operations

Why this matters:

- Enriched documents should identify the same story across different URLs while
  remaining conservative, privacy-safe, explainable, and maintainable.

Acceptance criteria:

- After enrichment, the worker searches eligible prior public-channel documents
  from the preceding 45 days and classifies identical content before semantic
  similarity.
- Semantic candidates require distinct normalized URLs, distinct public channels,
  usable extraction quality, a configured high threshold, and are labelled as
  potential rather than certain duplicates.
- The worker atomically reserves and finalizes one alert for the new message;
  deterministic or identical-content evidence wins over semantic evidence.
- Message edits reconcile removed/added links and requeue changed content;
  deletions cancel related jobs, associations, and posted alerts while retaining
  documents referenced elsewhere.
- README/operator documentation records environment controls, safe defaults,
  lifecycle, failure degradation, and limitations.

Likely touchpoints (non-exhaustive):

- enrichment and duplicate orchestration modules
- `archivebot.py`, `gunicorn_conf.py`, `utils.py`
- `README.md`
- integration-style unit tests with fake fetch, embed, and Slack clients

Verification:

```bash
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests/test_link_enrichment.py tests/test_link_duplicates.py tests/test_utils.py
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run python -m py_compile archivebot.py flask_app.py gunicorn_conf.py link_enrichment.py link_duplicates.py utils.py
```

Status: Not started.

## Final Verification

Run from `/Users/smarzola/projects/slack-archive-bot`:

```bash
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv lock --check
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv sync --frozen
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run pytest tests
UV_CACHE_DIR=/private/tmp/slack-archive-bot-goal-uv-cache uv run python -m py_compile archivebot.py flask_app.py gunicorn_conf.py link_enrichment.py link_duplicates.py utils.py
git diff --check origin/master...HEAD
```

Inspect all failures and repair in-scope defects rather than weakening tests. An
unrelated pre-existing failure is acceptable only with the command, output, and
evidence that this branch did not cause it.

## Resume Protocol

On resume, read this prompt, `AGENTS.md`, `git status`, milestone status notes,
and recent commits. Verify completed checkpoints and continue from the first
unchecked milestone without redoing completed work. New evidence may refine
implementation details but must not silently weaken target state or criteria.

## Final Report

Lead with `Achieved` or `Not achieved`, then report the goal file, branch, target
state and success-criteria status, milestone checkpoint commits, changed files,
exact verification results, reviewer rounds and disposition, residual risks,
and the external push/PR step that remains unauthorized.

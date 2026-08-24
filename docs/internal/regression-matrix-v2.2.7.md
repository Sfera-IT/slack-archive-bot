# Regression matrix v2.2.7

Date: 2026-08-24

Baseline under review: the changes released from v2.0.0 through v2.2.6, with
particular attention to the Responses API migration introduced in v2.1.0 and
the rollback/recovery sequence through v2.2.6.

## Release blockers

| Area | Regression or contract | Evidence | Result |
| --- | --- | --- | --- |
| Engaged threads | The live Slack event must remain the AI trigger even when the author opted out of archive persistence | `test_engage_passes_live_trigger_when_archive_optout_removes_all_context`, `test_archive_optout_redacts_storage_but_preserves_live_engage_trigger` | Pass |
| Engaged threads | The triggering text, user and timestamp must be passed to the grounded agent instead of inferred from the last archived message | `test_auto_reply_uses_trigger_instead_of_latest_visible_context_author` | Pass |
| Engaged threads | Duplicate and out-of-order Slack deliveries must not generate duplicate replies | Existing engage claim, stale-event and rollback tests | Pass |
| AI privacy | AI opt-out must be explicit and must consume neither an event claim nor rate-limit quota | `test_engage_ai_optout_is_explicit_and_consumes_no_claim_or_quota` | Pass |
| Archive privacy | Archive opt-out remains an opt-out: persisted text, author and permalink stay redacted while the separate live AI trigger remains available | `test_archive_optout_redacts_storage_but_preserves_live_engage_trigger` | Pass |
| Archive privacy | Links authored by an archive-opted-out user must not create link-enrichment persistence | Same archive opt-out regression test | Pass |
| Direct mention | Mention replies still use the grounded Responses API agent, the real requester and the visible rate-limit footer | `test_direct_mention_keeps_responses_agent_request_and_rate_footer` | Pass |
| Automatic engage/clown | Structured Responses API calls keep model, reasoning and JSON response contracts | `test_auto_engage_decision_uses_responses_helper_contract`, `test_auto_clown_decision_uses_responses_helper_contract` | Pass |
| Grounded agent | Responses API tool loop, encrypted reasoning continuity, final synthesis, limits, diagnostics and SDK serialization | `tests/test_ai_agent.py`, `tests/test_ai_diagnostics.py` | Pass |
| Digest | `/generate_digest` still calls Responses API for digest and podcast text, persists both, and reuses a recent cached digest | `test_generate_digest_and_podcast_use_responses_api_and_persist_results`, `test_generate_digest_reuses_recent_cached_result_without_openai` | Pass |
| Digest details | `/digest_details` still calls Responses API and persists question, answer and digest timestamp | `test_digest_details_uses_responses_api_and_persists_history` | Pass |
| Archive chat | `/chat` keeps the legacy inline `context` contract and Responses API call | `test_chat_uses_responses_api_and_returns_updated_conversation` | Pass |
| Published archive chat | The live frontend sends bounded `context_refs`; the backend must resolve them server-side and exclude archive/AI opt-outs | `test_chat_resolves_active_frontend_context_refs_server_side`, `test_chat_context_refs_exclude_archive_and_ai_optouts`, `test_chat_rejects_oversized_context_refs_before_openai` | Fixed and pass |
| Published thread view | The live frontend calls `/thread/<channel>/<thread_ts>` while the recovered backend exposed only `/thread/<message_id>` | `test_exact_thread_route_matches_active_frontend_contract`; live v2.2.6 returned 404 before this fix | Fixed and pass |
| Legacy thread view | `/thread/<message_id>` remains present for old clients | Full Flask route/test suite | Pass |
| Podcast download | Podcast content and MP3 routes retain their existing response contract | `test_podcast_content_and_audio_routes_keep_existing_contract` | Pass |
| Link handling | Link routing, normalization, enrichment, duplicate suppression and xcancel behavior | `tests/test_link_enrichment.py`, `tests/test_link_duplicates.py`, `tests/test_url_cleaner.py`, `tests/test_xcancel.py` | Pass |
| Member refresh | Repeated `member_joined_channel` delivery must be idempotent; a bot join refreshes the complete current membership snapshot | `test_member_joined_channel_retry_is_idempotent`, `test_bot_join_refreshes_channel_membership_snapshot` | Fixed and pass |
| SQLite schema | `members(channel,user)` is a set and must have one canonical UNIQUE index, deduplicating rollback-era databases once | `test_migrate_db_creates_hot_path_indexes`, `test_migrate_db_deduplicates_members_before_enforcing_unique_index` | Fixed and pass |
| Docker startup | Image starts with four Gunicorn workers and four enrichment workers; `/healthz` returns 200; SQLite `quick_check` is `ok` | Local image smoke test with a temporary database | Pass |
| Backend suite | Dependency lock, Python compilation, whitespace and complete tests | `uv lock --check`; tracked Python `py_compile`; `git diff --check`; `uv run pytest -q` | 171 passed |
| Published frontend | Active `origin/main` checkout installs, tests and builds against the backend contracts | `npm ci`; `CI=true npm test -- --runInBand`; `npm run build` | 7 suites / 20 tests passed; build passed |
| Published assets | The public archive serves the same JS/CSS hashes produced by the active frontend commit | `main.c49b8578.js`, `main.7685926f.css` | Pass |

## Production database read-only audit

The database and both server-side copies were opened read-only through the
ClawGuard SSH gateway. No production database write or manual schema change was
performed, so no additional backup was necessary.

| Check | Current database | `slack.sqlite.broken` | `slack.sqlite.bk20260824` |
| --- | --- | --- | --- |
| `PRAGMA quick_check` | `ok` | `ok` | `ok` |
| Canonical member index | UNIQUE | UNIQUE | UNIQUE |
| Duplicate memberships | 0 | 0 | 0 |
| Broken membership references | 0 | 0 | 0 |
| First 1001 foreign-key violations | 0 | 0 | 0 |
| Privacy migration marker | Present | Present | Present |
| Messages belonging to current archive opt-outs | 0 | 0 | 0 |

At observation time the live database had 1,322,721 messages, 2,197 unique
memberships, 12 archive opt-outs, 6 AI opt-outs and 7 engaged threads. Both
copies had 1,322,606 messages and the same clean membership/privacy invariants.
They are already post-v2.2.0 copies, therefore they cannot quantify deletions
against a pre-v2.2.0 baseline. The current database only showed expected later
message and link-enrichment growth.

## Deliberate exclusions

- Archive opt-out behavior was preserved and was not converted to opt-in.
- Private-channel result filtering and backend per-channel authorization were
  not added: the installation intentionally archives public channels only.
- `/healthz` was verified but no liveness/readiness redesign was introduced.
- The embeddings dependency warning and its existing fallback were not changed.
- The existing hard-coded bot identifier fallback was not changed.
- The unpublished 16-file working patch remains isolated on
  `quarantine/v2.2.2-unpublished-16-file-patch` and is absent from this release.

## Non-blocking observations

- The active frontend declares Node 24 while this audit ran with Node 26; tests
  and the production build still passed.
- `npm ci` reported dependency vulnerabilities already present in the frontend
  lockfile. Dependency/security remediation was outside this regression release.

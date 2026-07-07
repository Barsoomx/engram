# Context Relevance Activation

Date: 2026-07-07. Owner: team lead session. Source: comparative review of
engram plugin/MCP<->backend vs claude-mem (thedotmack/claude-mem v13.10.2),
verified against master `b3e6c572` and prod DB at engram.tools.byster.one.

## Problem (verified)

1. The user prompt is never forwarded as the retrieval query.
   `build_user_prompt_submit_payload` reads `input_payload['query']`
   (packages/cli/engram_cli/commands.py:1223) but Claude Code sends the text
   in the `prompt` field. Prod: 318/318 `user_prompt_submit` bundles have
   empty `query_text`; every doc scores 1 `filter-only authorized memory`;
   30 most recent bundles collapse to 3 distinct memory sets (one per
   project); ~89% of 7-day injections repeat a prior set. The whole hybrid
   retrieval pipeline (semantic 30 / lexical 20 / exact 100-40 tiers) is
   inert for hooks.
2. No gating: 3-word prompts get the same 5-memory injection, 210-1138 ms
   retrieval latency each, two sequential blocking POSTs per prompt.
3. `rendered_context` is duplicated into `systemMessage` and
   `additionalContext`; empty result still injects a stub
   ('No approved memory matched this request.').
4. SessionStart matcher `startup|resume` misses `clear`/`compact` sources —
   sessions are not re-primed after /clear or auto-compact.
5. Confidence is never a retrieval signal (decay is cosmetic); digests win
   rank-1 in 124/289 bundles by recency alone.
6. Nothing nudges the model to search memory: 0 MCP `engram_search` calls in
   32h of API logs; tool descriptions are passive one-liners.

## Reference points from claude-mem

- Per-prompt semantic inject is opt-in, gated to prompts >= 20 chars on both
  client and server; short prompts get nothing.
- Session start = recency timeline with two-track render (compact markdown
  index for the model, colored summary for the human), progressive
  disclosure (index + "fetch detail via tools"), explicit search nudges
  inside the injected context and 'Step 1/2/3' tool descriptions.
- All hooks fail open (exit 0) when the worker is down.

## Decisions

D1. Forward prompt as query (client). In
    `build_user_prompt_submit_payload`, fall back `query -> prompt`.
    `session_start` keeps an empty query (recency prime is intentional).
D2. Client-side gating (client). Skip the context POST (but keep the hook
    ingest POST) when the stripped prompt is shorter than 20 chars or starts
    with `<command-` or `/`. Output `{}` so nothing is injected.
D3. Suppress empty injections (client). `user-prompt-submit`: when the
    context response has no items, output `{}` (no additionalContext, no
    systemMessage). `session-start`: when empty, emit a one-line friendly
    systemMessage only.
D4. Bound injection size (client). Send `token_budget` 1200 for
    user_prompt_submit and 2000 for session_start.
D5. SessionStart matcher -> `startup|resume|clear|compact` (client).
D6. Quiet failures (client). On transport errors in hook commands, print the
    first error, then suppress repeats for a cooldown window (marker file in
    tempdir); always exit 0 for hook paths so prompts are never annotated
    with stderr noise on every turn.
D7. Trim contains-tier noise (server). In the exact_terms (60) and full_text
    (40) tiers, only consider query tokens of length >= 4 (whole-prompt term
    stays). Prevents Russian/English stopwords from producing spurious
    exact-tier hits that outrank semantic once D1 lands.
D8. Purpose-aware empty render (server). For `user_prompt_submit` with zero
    matches, `rendered_text` becomes '' (no stub). `session_start` keeps an
    informative empty state.
D9. Filter-only dump hygiene (server). In the no-terms (score=1) path, cap
    digests to the single most recent one; remaining slots go to non-digest
    memories by recency.
D10. Confidence as tiebreak (server). Within equal score tiers sort by
     (-score, -confidence, -updated_at, ...) so confidence decay affects
     ranking.
D11. Directive MCP descriptions + search nudge (client, later slice).
     Rewrite `engram_search`/`engram_observations` descriptions with an
     imperative 'run before starting non-trivial tasks' framing; append a
     search nudge line to injected session-start context; re-cue the
     mem-search skill to task starts and point it at the MCP tool.
D12. Two-track session-start render (client, later slice). Model gets a
     compact index (title/kind/age per memory + how to dig deeper); human
     systemMessage becomes a one-line summary instead of the full dump.

Out of scope (recorded for gaps ledger): merging the two hook POSTs into one
endpoint; PreToolUse(Read) file-context injection; making decay archive
memories; near-duplicate memory dedup; kind coverage (58% empty on prod).

## Slices

S1 client `feat/context-query-forwarding`: D1-D6, bundled plugin runtime
   byte-sync, tests. Owner: client worker.
S2 backend `feat/context-relevance-guardrails`: D7-D10, tests. Owner:
   backend worker.
S3 client `feat/session-start-render`: D11-D12 after S1/S2 merge.

Ops (no code): top up DeepSeek billing (402 since 2026-07-06); enable
`lexical_recall_enabled` for the prod org; redeploy after merges.

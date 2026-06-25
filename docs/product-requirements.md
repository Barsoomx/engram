# Product Requirements

## Vision

The product vision is defined by [North Star](north-star.md). This document
turns that vision into product requirements for the first server-side rewrite.

Engram is the memory layer between codebases and AI development agents.
V1 should make daily agent work faster by reducing repeated project re-learning,
not by becoming a broad knowledge-base platform.

## Primary Users

- Developer: wants agent sessions to remember project decisions, incident
  lessons, local conventions, and review outcomes without manual copy-paste.
- Agent: needs a compact, task-relevant context bundle before changing code.
- Team lead: wants a daily AI-generated summary of what changed, what is
  blocked, and what memory changed, without manually reviewing raw observations.
- Platform admin: wants on-premise deployment, provider key control, RBAC, audit
  logs, and simple rollout across Claude Code and Codex.
- Security admin: wants no secrets in memory, scoped API keys, retention rules,
  review queues, and an answer to "who could see this memory?"
- Memory curator: is an AI workflow that automatically rejects noise, merges
  duplicates, marks refuted or contradictory memory, and escalates only critical
  or ambiguous changes for human review.
- Auditor: wants immutable evidence for memory reads, writes, policy decisions,
  and secret access.

## Core Jobs

1. Capture observations from agent lifecycle hooks.
2. Generate durable memories from observations.
3. Assemble compact context bundles at session start, before tool use, after
   important tool use, and on explicit agent request.
4. Update memory when the agent discovers a changed fact, a resolved issue, or a
   new project convention.
5. Enforce tenant/team/project access before any memory is read, written, or
   packed into context.
6. Keep model provider secrets server-side and scoped to the owning organization
   or team.
7. Give admins a clear operational surface for access, configuration, review,
   audit, and deployment.
8. Provide a small client connect wizard that writes server-backed hooks without
   installing a local memory worker.
9. Run scheduled AI workflows that produce team digests and curate memory with
   exception-based human review.

## Hook-Centric Behavior

Hooks are not an optional notification channel. They are the deterministic
transport and control plane for the memory product.

Required hook responsibilities:

- Session start: resolve identity, tenant, team, project, repository, branch,
  and working directory; inject the initial memory bundle with citations.
- Pre-tool use: optionally retrieve focused memory, run secret/path guards, and
  attach guidance before high-risk operations.
- Post-tool use: capture observations, command outcomes, file references, error
  signatures, and review decisions.
- User prompt submit: expand prompt context when the user asks about prior work,
  recurring bugs, architecture, or conventions.
- Stop/session end: summarize unresolved findings, create candidate memories,
  mark stale context, and schedule background distillation.
- Explicit tools: expose memory search, observation lookup, memory update, and
  context feedback as agent-callable commands.

Policy enforcement is a mode that uses the same hook surfaces. Memory hooks must
not pretend to be the only security boundary; server authorization, audit, and
policy decisions remain authoritative.

## Business Capabilities

- Multi-tenant SaaS and single-tenant on-premise operation.
- Organization and team management.
- Project and repository binding.
- User invitations, service accounts, and scoped API keys.
- Team-owned AI provider secrets and model routing.
- AI memory workflow: proposed, rejected, merged, approved, refuted,
  conflicted, archived, escalated.
- Memory visibility: private user memory, team memory, project memory,
  organization memory, shared packs, and policy packs.
- Exact and semantic retrieval with permission filtering before context packing.
- Context bundle generation as the primary agent-facing output.
- Admin audit trail for reads, writes, retrieval decisions, hook calls, and
  secret usage.
- Export, retention, legal hold, and delete workflows suitable for enterprise
  customers.

## Non-Goals For The First Rewrite

- Local memory worker runtime.
- Desktop-only viewer as the primary operational UI.
- Agent-specific business logic duplicated outside the server.
- Complex cloud-style conditional access language.
- Vector-only retrieval.
- Search results as the main product output.
- Knowledge-base workflows that do not feed agent context.
- Multi-region active-active control plane.
- Manual review of every observation or memory proposal.

## Success Criteria

- A developer can install Claude Code and Codex hooks that only call the company
  server; no local worker starts.
- The client installer asks for server URL and identity/scope credentials; it
  does not deploy the server or install local summarization infrastructure.
- A team can configure provider keys and model policy without exposing raw
  secrets to agents.
- A single-team developer cannot read another team's memory unless a shared
  project, memory pack, or explicit grant allows it.
- Every injected memory has provenance, scope, and audit evidence.
- Every context bundle is explainable: why each memory was included, which scope
  allowed it, and which source supports it.
- Operators can deploy the stack on-premise with PostgreSQL and a queue, then
  scale retrieval and distillation independently.
- The first implementation remains small enough to reason about: few domain
  concepts, explicit contracts, and boring operational dependencies.
- Team leads receive daily or twice-daily digests with changelog, problems,
  unresolved questions, and memory changes.
- The AI curator can archive obvious noise and stale/refuted memories without a
  human queue, while escalating high-risk contradictions.

# Search And Retrieval

## Principle

Exact search is the baseline. Semantic search is recall expansion, not the
source of truth.

Agent memory needs reliable answers to questions like "what did we decide about
this file?", "where did this error happen?", and "which review found this bug?"
Those questions depend on filenames, symbols, ticket ids, commands, error
strings, and exact phrases. Vector search alone is not enough.

## Indexes

PostgreSQL should store normalized retrieval documents with:

- memory id and version;
- tenant/team/project/repository scope;
- source observation ids;
- file paths;
- symbols;
- branch/environment filters;
- normalized exact terms;
- full-text document;
- trigram-friendly fields;
- embedding reference.

V1 stack:

- PostgreSQL full-text search;
- `pg_trgm` for fuzzy exact strings;
- deterministic ranking;
- citations and audit.

Later:

- `pgvector` for embeddings;
- Qdrant adapter for customers that need separate vector scaling;
- model reranking.

## Retrieval Pipeline

1. Parse request intent and context.
2. Resolve actor and effective scope.
3. Build permission filters before querying content.
4. Run exact, full-text, and trigram retrieval.
5. Rank candidates deterministically.
6. Pack context with citations, confidence, source references, and stale/conflict
   warnings.
7. Audit the final injected memory set.

## Result Types

- memory: approved durable knowledge;
- observation: raw or lightly normalized session evidence;
- decision: approved project or team decision;
- incident: failure pattern and resolution;
- convention: local project rule or coding pattern;
- policy: admin guidance or guardrail;
- conflict: competing memories requiring review.

## Explainability

Every response to an agent should be explainable:

- matched exact terms;
- semantic neighbors used;
- scope filters applied;
- memory versions returned;
- stale or conflict markers;
- source observation links;
- model policy used for reranking or summarization.

This is required for user trust and for debugging bad memory injection.

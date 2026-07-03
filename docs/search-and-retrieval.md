# Search And Context Assembly

## Principle

The product goal is context assembly, not search results. Search is an internal
capability used to build the smallest useful context bundle for a concrete agent
task.

Retrieval must combine exact/grep-style matching with semantic retrieval in V1.
Exact matching is authoritative for file paths and memory titles today; other
categories (symbols, ticket ids, commands, error strings) fall back to
full-text substring matching because no memory-write path currently populates
the `symbols`/`exact_terms` metadata needed to index them. Semantic retrieval
is required for recall across paraphrases and related decisions. Neither path
is sufficient alone.

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
- normalized exact terms;
- full-text document;
- trigram-friendly fields;
- embedding reference.

V1 stack:

- PostgreSQL full-text search;
- `pg_trgm` for fuzzy exact strings;
- `pgvector` or equivalent in-PostgreSQL vector storage;
- embedding generation as a server-side worker job using the organization/team
  model policy, with OpenAI embeddings supported in V1;
- hybrid result fusion;
- deterministic ranking;
- citations and audit.

Later:

- Qdrant adapter for customers that need separate vector scaling;
- model reranking.

## Context Assembly Pipeline

1. Parse request intent and context.
2. Resolve actor and effective scope.
3. Build permission filters before querying content.
4. Run exact, full-text, trigram, and vector retrieval.
5. Fuse candidates deterministically, with exact matches allowed to dominate
   when filenames, symbols, ticket ids, commands, or error strings match.
6. Pack the context bundle with citations, source references, and inclusion
   reasons.
7. Audit the final injected memory set.

## Explainability

Every context bundle sent to an agent should be explainable:

- matched exact terms;
- semantic neighbors used and embedding model/version;
- scope filters applied;
- memory versions returned;
- source observation links;
- model policy used for reranking or summarization.

This is required for user trust and for debugging bad memory injection.

# Search And Context Assembly

## Principle

The product goal is context assembly, not search results. Search is an internal
capability used to build the smallest useful context bundle for a concrete agent
task.

Retrieval must combine exact/grep-style matching with semantic retrieval in V1.
Exact matching is authoritative for file paths and memory titles today.
Symbols and exact terms are derived deterministically at index time from a
memory's title and body: backticked identifiers and call forms, dotted paths,
and CamelCase/snake_case tokens become symbols; ticket ids, error classes,
UPPER_SNAKE constants, and backticked non-identifier commands become exact
terms. Extraction is regex-based, not semantic, and merges with any explicit
values already present in `Memory.metadata['symbols']`/`Memory.metadata['exact_terms']`.
The symbol tier (score 80) and exact-terms tier (score 60) rank between
file-path matches (score 100) and full-text substring matching (score 40).
`RetrievalDocument` rows indexed before this extraction landed can be
recomputed with the operator command `engram_backfill_retrieval_terms`.
Semantic retrieval is required for recall across paraphrases and related
decisions. Neither path is sufficient alone.

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

# Celery SLA Compose Topology Security Review

Date: 2026-06-25

Branch: `chore/celery-sla-compose-topology`

Result: pass.

## Scope Reviewed

- `deploy/compose/docker-compose.yml`
- `deploy/compose/README.md`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/superpowers/specs/2026-06-25-celery-sla-compose-topology-design.md`
- `docs/superpowers/plans/2026-06-25-celery-sla-compose-topology.md`

The focused review covers local deployment worker topology only. It does not
change task payloads, hook authentication, memory worker behavior, broker
credentials, or the `django-celery-outbox` transport contract.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| Delegated RED repository contract | Exit 1 before implementation. Worker reported missing `worker-realtime` and queue routing assertions against the old single generic worker. |
| Repository runtime contract | Exit 0. `python3 -m unittest tests.repository.test_backend_runtime_contract -v` reported 15 tests OK. |
| Compose service graph | Exit 0. `docker compose -f deploy/compose/docker-compose.yml config --quiet` produced no output. |
| Backend Celery foundation tests | Exit 0. `cd apps/backend && poetry run pytest engram/core/celery_foundation_tests.py -v` reported 7 passed. |
| Repository tests | Exit 0. `python3 -m unittest discover -s tests -v` reported 31 tests OK. |
| Repository layout | Exit 0. `python3 scripts/repository_layout.py` produced no output. |
| Repository text quality | Exit 0. `python3 scripts/repository_quality.py` produced no output. |
| Whitespace | Exit 0. `git diff --check HEAD` produced no output. |
| Compose golden path | Exit 0. `python3 scripts/e2e_golden_path.py` completed through worker-created retrieval document and future context injection, then stopped Compose services. |
| Independent read-only review agent | Exit 0. Reported no Critical, Important, or Minor findings. Verified SLA queue coverage, separate relay, unchanged RabbitMQ/Redis broker/result configuration, no public-doc private reference leakage, and sufficient Compose/E2E evidence. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

None.

### MINOR

None.

## Required Security Properties

- Compose uses RabbitMQ as the Celery broker and Redis only as result/cache
  backend.
- `django-celery-outbox` relay remains a separate process and is not replaced
  by a custom domain outbox worker.
- Dedicated workers cover every declared Engram SLA queue:
  `engram-realtime`, `engram-near-realtime`, `engram-batch`,
  `engram-highmemory`, and `engram-domain-events`.
- The default memory task path remains consumable by `engram-near-realtime`.
- No raw secrets, private reference names, or product-specific queue names are
  introduced in public docs.

## Accepted Risk

None.

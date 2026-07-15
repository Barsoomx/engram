from __future__ import annotations

from dataclasses import dataclass

from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork


@dataclass(frozen=True, slots=True)
class ExpireStaleCandidatesResult:
    scanned: int
    queued: int = 0
    # Kept as a read-only compatibility field for callers of the former TTL API.
    rejected: int = 0


class ExpireStaleCandidates:
    def execute(self) -> ExpireStaleCandidatesResult:
        result = ReconcileCandidateDecisionWork().execute()

        return ExpireStaleCandidatesResult(scanned=result.scanned, queued=result.queued)

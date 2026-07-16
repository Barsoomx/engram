from __future__ import annotations

from dataclasses import dataclass

from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork


@dataclass(frozen=True, slots=True)
class ExpireStaleCandidatesResult:
    scanned: int
    rejected: int


class ExpireStaleCandidates:
    def execute(self) -> ExpireStaleCandidatesResult:
        result = ReconcileCandidateDecisionWork().execute()

        return ExpireStaleCandidatesResult(scanned=result.scanned, rejected=0)

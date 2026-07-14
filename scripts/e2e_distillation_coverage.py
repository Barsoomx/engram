from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = (ROOT / "deploy/compose/docker-compose.yml").resolve()

EXPECTED_OBSERVATIONS = 101
GLOBAL_TIMEOUT = 25 * 60.0
WORKLOAD_TIMEOUT = 20 * 60.0
COMMAND_TIMEOUT = 180.0
STARTUP_TIMEOUT = 600.0
CLEANUP_TIMEOUT = 180.0
POLL_INTERVAL = 0.5
FAULT_POLL_ITERATIONS = 240
QUIESCENCE_ITERATIONS = 480
OUTPUT_LIMIT = 4000
MAX_FAKE_PROVIDER_DELAY_MS = 5000
FAULT_FAKE_PROVIDER_DELAY_MS = 2500

PROJECT_PATTERN = re.compile(r"engram-cp3-coverage-[0-9a-f]{16}\Z")
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{16}\Z")
_PROVIDER_MODES = frozenset({"fake", "real"})
_DISTILLATION_TASKS = (
    "engram.memory.distill_session_work_v1",
    "engram.memory.process_candidate_decision_work_v1",
)


class HarnessError(Exception):
    pass


class CommandFailure(HarnessError):
    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True, slots=True)
class Arguments:
    project: str


@dataclass(frozen=True, slots=True)
class ScopeIds:
    organization_id: str
    project_id: str
    team_id: str
    session_id: str
    work_id: str


@dataclass(frozen=True, slots=True)
class ObservationRef:
    observation_id: str
    session_sequence: int


@dataclass(frozen=True, slots=True)
class ManifestMembership:
    observation_id: str
    session_sequence: int
    chunk_ordinal: int


@dataclass(frozen=True, slots=True)
class CoverageRow:
    observation_id: str
    session_sequence: int
    outcome: str
    source_count: int
    deciding_stage_complete: bool


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    candidate_id: str
    content_hash: str
    work_count: int


@dataclass(frozen=True, slots=True)
class InvariantSnapshot:
    state: str
    reason: str


@dataclass(frozen=True, slots=True)
class DistillationState:
    root_work_count: int
    window_count: int
    root_disposition: str
    root_execution_state: str
    useful_observations: tuple[ObservationRef, ...]
    chunk_observation_counts: tuple[int, ...]
    manifest_memberships: tuple[ManifestMembership, ...]
    attempt_count: int
    continuation_package_count: int
    active_attempt_count: int
    worker_lost_attempt_count: int
    provider_transient_attempt_count: int
    extract_target_count: int
    extract_complete_target_count: int
    reduce_target_count: int
    reduce_complete_target_count: int
    pending_target_count: int
    coverage_rows: tuple[CoverageRow, ...]
    truncated_audit_count: int
    candidate_generations: tuple[CandidateGeneration, ...]
    pending_outbox_count: int
    invariants: dict[str, InvariantSnapshot]


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ReconciliationHarness(Protocol):
    def reconcile_once(
        self, scope: ScopeIds, *, timeout: float
    ) -> dict[str, object]: ...

    def query_state(self, scope: ScopeIds, *, timeout: float) -> DistillationState: ...


class Deadline:
    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        label: str = "global deadline",
    ) -> None:
        if timeout <= 0:
            raise HarnessError("deadline timeout must be positive")
        self._clock = clock
        self._expires_at = clock() + timeout
        self._label = label

    def remaining(self) -> float:
        remaining = self._expires_at - self._clock()
        if remaining <= 0:
            raise HarnessError(f"Harness exceeded its {self._label}")

        return remaining


def validate_project_name(project: str) -> None:
    if PROJECT_PATTERN.fullmatch(project) is None:
        raise HarnessError(f"Refusing unsafe disposable Compose project {project!r}")


def _validate_run_id(run_id: str) -> None:
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise HarnessError(f"Refusing unsafe run id {run_id!r}")


def _validate_absolute_canonical(path: Path, label: str) -> None:
    if not path.is_absolute() or path != path.resolve():
        raise HarnessError(f"{label} must be an absolute canonical path")


def _single_line(value: str, label: str) -> str:
    if not value or "\n" in value or "\r" in value:
        raise HarnessError(f"{label} must be a non-empty single line")

    return value


def _uuid_text(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise HarnessError(f"{label} must be a UUID") from error

    return str(parsed)


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(
        description="Run the disposable CP3 distillation coverage fault harness."
    )
    parser.add_argument(
        "--project",
        default="",
        help="Optional exact disposable project name for reproduction.",
    )
    parsed = parser.parse_args(argv)
    project = str(parsed.project) or f"engram-cp3-coverage-{secrets.token_hex(8)}"
    validate_project_name(project)

    return Arguments(project=project)


def compose_prefix(
    *,
    project: str,
    compose_file: Path,
    env_file: Path,
    override_file: Path,
) -> list[str]:
    validate_project_name(project)
    _validate_absolute_canonical(compose_file, "Compose file")
    _validate_absolute_canonical(env_file, "generated env file")
    _validate_absolute_canonical(override_file, "generated Compose override")

    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        project,
        "-f",
        str(compose_file),
        "-f",
        str(override_file),
    ]


def cleanup_command(
    *,
    project: str,
    compose_file: Path,
    env_file: Path,
    override_file: Path,
) -> list[str]:
    return [
        *compose_prefix(
            project=project,
            compose_file=compose_file,
            env_file=env_file,
            override_file=override_file,
        ),
        "down",
        "-v",
        "--remove-orphans",
    ]


def deterministic_env(
    env_file: Path,
    *,
    source: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    _validate_absolute_canonical(env_file, "generated env file")
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_RUNTIME_DIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    )
    result = {name: source[name] for name in allowed if source.get(name)}
    result.update(
        {
            "LC_ALL": "C.UTF-8",
            "COMPOSE_ANSI": "never",
            "ENGRAM_ENV_FILE": str(env_file),
        }
    )

    return result


def write_env_file(path: Path, *, provider_mode: str) -> None:
    _validate_absolute_canonical(path, "generated env file")
    if provider_mode not in _PROVIDER_MODES:
        raise HarnessError(f"Unsupported disposable provider mode {provider_mode!r}")
    path.write_text(
        "\n".join(
            [
                "ENGRAM_ENVIRONMENT=dev",
                "ENGRAM_SECRET_KEY=",
                "ENGRAM_DEBUG=false",
                "ENGRAM_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0",
                "ENGRAM_LOG_LEVEL=INFO",
                f"ENGRAM_PROVIDER_MODE={provider_mode}",
                "ENGRAM_PROVIDER_HTTP_TIMEOUT=3",
                "ENGRAM_DISTILL_CHUNK_CHAR_BUDGET=8000",
                "ENGRAM_DISTILL_CHUNK_CHAR_CEILING=8000",
                "ENGRAM_DISTILL_REDUCE_TARGET=1",
                "ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT=2",
                "ENGRAM_DISTILL_SOFT_TIME_LIMIT=900",
                "ENGRAM_DISTILL_TIME_LIMIT=960",
                "ENGRAM_RABBITMQ_HOSTNAME=rabbitmq",
                "ENGRAM_RABBITMQ_NODENAME=rabbit@rabbitmq",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_override_file(
    path: Path,
    project: str,
    *,
    fake_provider_delay_ms: int = 0,
) -> None:
    _validate_absolute_canonical(path, "generated Compose override")
    validate_project_name(project)
    if (
        type(fake_provider_delay_ms) is not int
        or not 0 <= fake_provider_delay_ms <= MAX_FAKE_PROVIDER_DELAY_MS
    ):
        raise HarnessError(
            "fake provider delay must be an integer from 0 to 5000 milliseconds"
        )
    image = f"{project}-backend:cp3"
    path.write_text(
        "services:\n"
        "  api:\n"
        f"    image: {image}\n"
        "    ports: !override\n"
        '      - "127.0.0.1::8000"\n'
        "  worker-batch:\n"
        f"    image: {image}\n"
        "    environment:\n"
        f'      ENGRAM_FAKE_PROVIDER_DELAY_MS: "{fake_provider_delay_ms}"\n'
        "  relay:\n"
        f"    image: {image}\n",
        encoding="utf-8",
    )


def redact_diagnostics(value: str, generated_secrets: Sequence[str]) -> str:
    redacted = value
    for secret in sorted(
        {item for item in generated_secrets if item}, key=len, reverse=True
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?<![A-Za-z0-9])egk_[A-Za-z0-9_.-]+", "[REDACTED]", redacted)
    redacted = re.sub(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_.-]+", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?m)(Generated admin password:\s*)[^\r\n]+",
        r"\1[REDACTED]",
        redacted,
    )

    return redacted[-OUTPUT_LIMIT:]


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HarnessError(f"{label} keys are invalid")

    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise HarnessError(f"{label} must be a list")

    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise HarnessError(f"{label} must be an integer")

    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise HarnessError(f"{label} must be a string")

    return value


def _observation_ref(value: object, label: str) -> ObservationRef:
    row = _exact_object(value, {"observation_id", "session_sequence"}, f"{label} row")

    return ObservationRef(
        observation_id=_string(row["observation_id"], f"{label} observation_id"),
        session_sequence=_integer(row["session_sequence"], f"{label} session_sequence"),
    )


def _manifest_membership(value: object) -> ManifestMembership:
    row = _exact_object(
        value,
        {"observation_id", "session_sequence", "chunk_ordinal"},
        "manifest membership row",
    )

    return ManifestMembership(
        observation_id=_string(row["observation_id"], "manifest observation_id"),
        session_sequence=_integer(row["session_sequence"], "manifest session_sequence"),
        chunk_ordinal=_integer(row["chunk_ordinal"], "manifest chunk_ordinal"),
    )


def _coverage_row(value: object) -> CoverageRow:
    row = _exact_object(
        value,
        {
            "observation_id",
            "session_sequence",
            "outcome",
            "source_count",
            "deciding_stage_complete",
        },
        "coverage row",
    )
    deciding_stage_complete = row["deciding_stage_complete"]
    if type(deciding_stage_complete) is not bool:
        raise HarnessError("coverage deciding_stage_complete must be a boolean")

    return CoverageRow(
        observation_id=_string(row["observation_id"], "coverage observation_id"),
        session_sequence=_integer(row["session_sequence"], "coverage session_sequence"),
        outcome=_string(row["outcome"], "coverage outcome"),
        source_count=_integer(row["source_count"], "coverage source_count"),
        deciding_stage_complete=deciding_stage_complete,
    )


def _candidate_generation(value: object) -> CandidateGeneration:
    row = _exact_object(
        value,
        {"candidate_id", "content_hash", "work_count"},
        "candidate generation row",
    )

    return CandidateGeneration(
        candidate_id=_string(row["candidate_id"], "candidate_id"),
        content_hash=_string(row["content_hash"], "candidate content_hash"),
        work_count=_integer(row["work_count"], "candidate work_count"),
    )


def _invariant(value: object, invariant_id: str) -> InvariantSnapshot:
    row = _exact_object(value, {"state", "reason"}, f"{invariant_id} invariant row")

    return InvariantSnapshot(
        state=_string(row["state"], f"{invariant_id} state"),
        reason=_string(row["reason"], f"{invariant_id} reason"),
    )


_STATE_KEYS = {
    "root_work_count",
    "window_count",
    "root_disposition",
    "root_execution_state",
    "useful_observations",
    "chunk_observation_counts",
    "manifest_memberships",
    "attempt_count",
    "continuation_package_count",
    "active_attempt_count",
    "worker_lost_attempt_count",
    "provider_transient_attempt_count",
    "extract_target_count",
    "extract_complete_target_count",
    "reduce_target_count",
    "reduce_complete_target_count",
    "pending_target_count",
    "coverage",
    "truncated_audit_count",
    "candidate_generations",
    "pending_outbox_count",
    "invariants",
}


def parse_state(payload: str) -> DistillationState:
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as error:
        raise HarnessError("state output is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise HarnessError("state output must be a JSON object")
    data = _exact_object(decoded, _STATE_KEYS, "state object")
    invariants = _exact_object(data["invariants"], {"P3", "P5"}, "invariants object")

    return DistillationState(
        root_work_count=_integer(data["root_work_count"], "root_work_count"),
        window_count=_integer(data["window_count"], "window_count"),
        root_disposition=_string(
            data["root_disposition"], "root_disposition", allow_empty=True
        ),
        root_execution_state=_string(
            data["root_execution_state"],
            "root_execution_state",
            allow_empty=True,
        ),
        useful_observations=tuple(
            _observation_ref(value, "useful observation")
            for value in _list(data["useful_observations"], "useful")
        ),
        chunk_observation_counts=tuple(
            _integer(value, "chunk observation count")
            for value in _list(
                data["chunk_observation_counts"], "chunk observation counts"
            )
        ),
        manifest_memberships=tuple(
            _manifest_membership(value)
            for value in _list(data["manifest_memberships"], "manifest memberships")
        ),
        attempt_count=_integer(data["attempt_count"], "attempt_count"),
        continuation_package_count=_integer(
            data["continuation_package_count"], "continuation_package_count"
        ),
        active_attempt_count=_integer(
            data["active_attempt_count"], "active_attempt_count"
        ),
        worker_lost_attempt_count=_integer(
            data["worker_lost_attempt_count"], "worker_lost_attempt_count"
        ),
        provider_transient_attempt_count=_integer(
            data["provider_transient_attempt_count"],
            "provider_transient_attempt_count",
        ),
        extract_target_count=_integer(
            data["extract_target_count"], "extract_target_count"
        ),
        extract_complete_target_count=_integer(
            data["extract_complete_target_count"],
            "extract_complete_target_count",
        ),
        reduce_target_count=_integer(
            data["reduce_target_count"], "reduce_target_count"
        ),
        reduce_complete_target_count=_integer(
            data["reduce_complete_target_count"],
            "reduce_complete_target_count",
        ),
        pending_target_count=_integer(
            data["pending_target_count"], "pending_target_count"
        ),
        coverage_rows=tuple(
            _coverage_row(value) for value in _list(data["coverage"], "coverage")
        ),
        truncated_audit_count=_integer(
            data["truncated_audit_count"], "truncated_audit_count"
        ),
        candidate_generations=tuple(
            _candidate_generation(value)
            for value in _list(data["candidate_generations"], "candidate generations")
        ),
        pending_outbox_count=_integer(
            data["pending_outbox_count"], "pending_outbox_count"
        ),
        invariants={key: _invariant(value, key) for key, value in invariants.items()},
    )


def state_is_quiescent(state: DistillationState) -> bool:
    return (
        state.root_disposition == "complete"
        and state.root_execution_state == "settled"
        and state.active_attempt_count == 0
        and state.pending_target_count == 0
        and state.pending_outbox_count == 0
    )


def worker_loss_fault_window(state: DistillationState) -> bool:
    accepted_stage_count = (
        state.extract_complete_target_count + state.reduce_complete_target_count
    )

    return accepted_stage_count == 1 and state.active_attempt_count == 1


def assert_final_state(state: DistillationState) -> None:  # noqa: C901, PLR0915
    if state.root_work_count != 1:
        raise HarnessError(
            f"Expected exactly one root work, got {state.root_work_count}"
        )
    if state.window_count != 1:
        raise HarnessError(f"Expected exactly one window, got {state.window_count}")

    useful_pairs = [
        (row.observation_id, row.session_sequence) for row in state.useful_observations
    ]
    useful_ids = {row[0] for row in useful_pairs}
    useful_sequences = {row[1] for row in useful_pairs}
    expected_sequences = set(range(1, EXPECTED_OBSERVATIONS + 1))
    if (
        len(useful_pairs) != EXPECTED_OBSERVATIONS
        or len(useful_ids) != EXPECTED_OBSERVATIONS
        or useful_sequences != expected_sequences
    ):
        raise HarnessError(
            "Expected exactly 101 useful observations with sequences 1..101"
        )

    if len(state.chunk_observation_counts) != EXPECTED_OBSERVATIONS or any(
        count != 1 for count in state.chunk_observation_counts
    ):
        raise HarnessError("Expected exactly one observation per chunk")

    manifest_pairs = [
        (row.observation_id, row.session_sequence) for row in state.manifest_memberships
    ]
    manifest_ordinals = {row.chunk_ordinal for row in state.manifest_memberships}
    if (
        len(manifest_pairs) != EXPECTED_OBSERVATIONS
        or len(set(manifest_pairs)) != EXPECTED_OBSERVATIONS
        or set(manifest_pairs) != set(useful_pairs)
        or manifest_ordinals != set(range(EXPECTED_OBSERVATIONS))
    ):
        raise HarnessError(
            "manifest membership has a duplicate, gap, or foreign observation"
        )

    if state.attempt_count <= 1:
        raise HarnessError("Expected more than one root attempt")
    if state.continuation_package_count < 1:
        raise HarnessError("Expected durable continuation package history")
    if state.worker_lost_attempt_count < 1:
        raise HarnessError("Expected worker-loss attempt evidence")
    if state.provider_transient_attempt_count < 1:
        raise HarnessError("Expected provider-outage attempt evidence")

    if (
        state.extract_target_count != EXPECTED_OBSERVATIONS
        or state.extract_complete_target_count != state.extract_target_count
    ):
        raise HarnessError("One or more extraction targets are incomplete")
    if state.reduce_target_count < 1:
        raise HarnessError("Required reduction targets were not materialized")
    if state.reduce_complete_target_count != state.reduce_target_count:
        raise HarnessError("One or more reduction targets are incomplete")
    if state.pending_target_count:
        raise HarnessError(
            f"{state.pending_target_count} provider targets remain pending"
        )

    coverage_pairs = [
        (row.observation_id, row.session_sequence) for row in state.coverage_rows
    ]
    if len(coverage_pairs) != EXPECTED_OBSERVATIONS:
        raise HarnessError(
            f"Expected exactly 101 coverage rows, got {len(coverage_pairs)}"
        )
    if len(set(coverage_pairs)) != EXPECTED_OBSERVATIONS or set(coverage_pairs) != set(
        useful_pairs
    ):
        raise HarnessError(
            "coverage rows do not match the immutable useful observation set"
        )
    for row in state.coverage_rows:
        if not row.deciding_stage_complete:
            raise HarnessError("A coverage deciding stage is incomplete")
        if row.outcome == "signal":
            if row.source_count < 1:
                raise HarnessError(
                    "signal coverage must have at least one source relation"
                )
        elif row.outcome == "no_signal":
            if row.source_count != 0:
                raise HarnessError("no-signal coverage must not have a source relation")
        else:
            raise HarnessError(f"Coverage outcome is invalid: {row.outcome!r}")

    if state.truncated_audit_count:
        raise HarnessError("SessionDistillationTruncated audit must not be emitted")
    if not state.candidate_generations:
        raise HarnessError("Expected at least one candidate decision generation")
    if any(generation.work_count != 1 for generation in state.candidate_generations):
        raise HarnessError(
            "Expected exactly one current candidate-decision work per content identity"
        )
    for invariant_id in ("P3", "P5"):
        invariant = state.invariants[invariant_id]
        if invariant.state != "healthy":
            raise HarnessError(
                f"{invariant_id} is not healthy: {invariant.state}/{invariant.reason}"
            )

    if state.root_disposition != "complete" or state.root_execution_state != "settled":
        raise HarnessError("The root work is not settled")
    if state.active_attempt_count:
        raise HarnessError(f"{state.active_attempt_count} active root attempts remain")
    if state.pending_outbox_count:
        raise HarnessError(
            f"{state.pending_outbox_count} distillation delivery remains pending"
        )


def _python_literal(value: str) -> str:
    return json.dumps(value)


def seed_code(
    *,
    run_id: str,
    organization_id: str,
    project_id: str,
    team_id: str,
) -> str:
    _validate_run_id(run_id)
    organization_id = _uuid_text(organization_id, "organization_id")
    project_id = _uuid_text(project_id, "project_id")
    team_id = _uuid_text(team_id, "team_id")

    return dedent(
        f"""
        import hashlib
        import json
        import uuid
        from django.utils import timezone
        from engram.core.models import Agent, AgentSession, Observation, Runtime
        from engram.memory.session_lifecycle import EndSession

        organization_id = uuid.UUID({_python_literal(organization_id)})
        project_id = uuid.UUID({_python_literal(project_id)})
        team_id = uuid.UUID({_python_literal(team_id)})
        run_id = {_python_literal(run_id)}
        agent, _created = Agent.objects.get_or_create(
            organization_id=organization_id,
            runtime=Runtime.CODEX,
            external_id=f'cp3-coverage-{{run_id}}',
            defaults={{'display_name': 'CP3 coverage agent', 'version': 'e2e'}},
        )
        session = AgentSession.objects.create(
            organization_id=organization_id,
            project_id=project_id,
            team_id=team_id,
            agent=agent,
            external_session_id=f'cp3-coverage-{{run_id}}',
            runtime=Runtime.CODEX,
            platform_source='cp3-e2e',
            started_at=timezone.now(),
        )
        Observation.objects.bulk_create([
            Observation(
                organization_id=organization_id,
                project_id=project_id,
                team_id=team_id,
                agent=agent,
                session=session,
                observation_type='tool_use',
                title=f'CP3 useful observation {{sequence}}',
                body=f'CP3 useful observation {{sequence}} for {{run_id}} ' + 'x' * 8500,
                content_hash=hashlib.sha256(f'{{run_id}}:{{sequence}}'.encode()).hexdigest(),
                session_sequence=sequence,
                source_metadata={{'event_type': 'post_tool_use', 'cp3_run_id': run_id}},
                observed_at=timezone.now(),
            )
            for sequence in range(1, 102)
        ])
        session.observation_sequence_cursor = 101
        session.save(update_fields=['observation_sequence_cursor', 'updated_at'])
        ended = EndSession().execute(
            organization_id=organization_id,
            project_id=project_id,
            session_id=session.id,
            ended_at=timezone.now(),
            source='explicit',
        )
        if ended.work_id is None or not ended.work_created or not ended.initial_signal_created:
            raise RuntimeError('CP3 seed did not create and signal one v1 root work')
        print(json.dumps({{
            'organization_id': str(organization_id),
            'project_id': str(project_id),
            'team_id': str(team_id),
            'session_id': str(session.id),
            'work_id': str(ended.work_id),
        }}, sort_keys=True))
        """
    ).strip()


def state_query_code(scope: ScopeIds) -> str:  # noqa: C901
    organization_id = _uuid_text(scope.organization_id, "organization_id")
    project_id = _uuid_text(scope.project_id, "project_id")
    session_id = _uuid_text(scope.session_id, "session_id")
    work_id = _uuid_text(scope.work_id, "work_id")

    return dedent(
        f"""
        import json
        import uuid
        from django.db.models import Count
        from django_celery_outbox.models import CeleryOutbox
        from engram.core.models import (
            AuditEvent,
            DistillationObservationCoverage,
            DistillationStage,
            DistillationWindow,
            MemoryCandidate,
            MemoryCandidateSource,
            Observation,
            WorkflowRun,
            WorkflowWork,
        )
        from engram.memory.invariant_queries import evaluate_invariants
        from engram.memory.observation_work import useful_observation_q

        organization_id = uuid.UUID({_python_literal(organization_id)})
        project_id = uuid.UUID({_python_literal(project_id)})
        session_id = uuid.UUID({_python_literal(session_id)})
        work_id = uuid.UUID({_python_literal(work_id)})
        roots = WorkflowWork.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            subject_type='agent_session',
            subject_id=session_id,
            work_type='session_distillation',
            contract_version=1,
        )
        root = roots.filter(id=work_id).first()
        windows = DistillationWindow.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            contract_version=1,
        )
        window = windows.filter(work_id=work_id).first()
        chunks = list(window.chunks.order_by('ordinal')) if window is not None else []
        manifest_memberships = []
        for chunk in chunks:
            entries = chunk.input_manifest.get('observations', []) if isinstance(chunk.input_manifest, dict) else []
            for entry in entries:
                manifest_memberships.append({{
                    'observation_id': entry.get('observation_id') if isinstance(entry, dict) else None,
                    'session_sequence': entry.get('session_sequence') if isinstance(entry, dict) else None,
                    'chunk_ordinal': chunk.ordinal,
                }})
        useful_observations = [
            {{'observation_id': str(item.id), 'session_sequence': item.session_sequence}}
            for item in Observation.objects.filter(
                organization_id=organization_id,
                project_id=project_id,
                session_id=session_id,
            ).filter(useful_observation_q()).order_by('session_sequence', 'id')
        ]
        stages = list(
            DistillationStage.objects.filter(
                organization_id=organization_id,
                project_id=project_id,
                window_id=window.id if window is not None else None,
            ).order_by('stage_kind', 'level', 'ordinal', 'id')
        ) if window is not None else []
        target_keys = {{stage.target_key for stage in stages}}
        complete_target_keys = {{stage.target_key for stage in stages if stage.status == 'complete'}}
        extract_target_keys = {{stage.target_key for stage in stages if stage.stage_kind == 'extract'}}
        reduce_target_keys = {{stage.target_key for stage in stages if stage.stage_kind == 'reduce'}}
        extract_complete_keys = extract_target_keys & complete_target_keys
        reduce_complete_keys = reduce_target_keys & complete_target_keys
        source_counts = {{}}
        if window is not None:
            source_counts = {{
                str(row['observation_id']): row['count']
                for row in MemoryCandidateSource.objects.filter(window_id=window.id)
                .values('observation_id')
                .annotate(count=Count('id'))
            }}
        coverage = []
        if window is not None:
            coverage = [
                {{
                    'observation_id': str(row.observation_id),
                    'session_sequence': row.session_sequence,
                    'outcome': row.outcome,
                    'source_count': source_counts.get(str(row.observation_id), 0),
                    'deciding_stage_complete': row.deciding_stage.status == 'complete',
                }}
                for row in DistillationObservationCoverage.objects.filter(window_id=window.id)
                .select_related('deciding_stage')
                .order_by('session_sequence', 'id')
            ]
        candidate_ids = []
        if window is not None:
            candidate_ids = list(
                MemoryCandidateSource.objects.filter(window_id=window.id)
                .values_list('candidate_id', flat=True)
                .distinct()
            )
        candidate_generations = []
        for candidate in MemoryCandidate.objects.filter(id__in=candidate_ids).order_by('id'):
            work_count = WorkflowWork.objects.filter(
                organization_id=organization_id,
                project_id=project_id,
                work_type='candidate_decision',
                subject_type='memory_candidate',
                subject_id=candidate.id,
                contract_version=1,
                input_snapshot__candidate_content_hash=candidate.content_hash,
            ).count()
            candidate_generations.append({{
                'candidate_id': str(candidate.id),
                'content_hash': candidate.content_hash,
                'work_count': work_count,
            }})
        attempts = WorkflowRun.objects.filter(work_id=work_id, execution_contract_version=1)
        invariant_results = {{
            result.invariant_id.value: {{'state': result.state.value, 'reason': result.reason}}
            for result in evaluate_invariants(
                organization_id=organization_id,
                project_id=project_id,
            )
            if result.invariant_id.value in ('P3', 'P5')
        }}
        payload = {{
            'root_work_count': roots.count(),
            'window_count': windows.count(),
            'root_disposition': root.disposition if root is not None else '',
            'root_execution_state': root.execution_state if root is not None else '',
            'useful_observations': useful_observations,
            'chunk_observation_counts': [chunk.observation_count for chunk in chunks],
            'manifest_memberships': manifest_memberships,
            'attempt_count': attempts.count(),
            'continuation_package_count': attempts.filter(origin='reconciliation').count(),
            'active_attempt_count': attempts.filter(status__in=('queued', 'running')).count(),
            'worker_lost_attempt_count': attempts.filter(failure_class='worker_lost').count(),
            'provider_transient_attempt_count': attempts.filter(failure_class='provider_transient').count(),
            'extract_target_count': len(extract_target_keys),
            'extract_complete_target_count': len(extract_complete_keys),
            'reduce_target_count': len(reduce_target_keys),
            'reduce_complete_target_count': len(reduce_complete_keys),
            'pending_target_count': len(target_keys - complete_target_keys),
            'coverage': coverage,
            'truncated_audit_count': AuditEvent.objects.filter(
                organization_id=organization_id,
                project_id=project_id,
                event_type='SessionDistillationTruncated',
                target_id=str(session_id),
            ).count(),
            'candidate_generations': candidate_generations,
            'pending_outbox_count': CeleryOutbox.objects.filter(task_name__in={_DISTILLATION_TASKS!r}).count(),
            'invariants': invariant_results,
        }}
        print(json.dumps(payload, sort_keys=True))
        """
    ).strip()


def reconcile_code(scope: ScopeIds) -> str:
    organization_id = _uuid_text(scope.organization_id, "organization_id")
    project_id = _uuid_text(scope.project_id, "project_id")
    session_id = _uuid_text(scope.session_id, "session_id")
    work_id = _uuid_text(scope.work_id, "work_id")

    return dedent(
        f"""
        import json
        import uuid
        from engram.core.models import AgentSession, WorkflowWork
        from engram.memory.tasks import retry_failed_distillations

        organization_id = uuid.UUID({_python_literal(organization_id)})
        project_id = uuid.UUID({_python_literal(project_id)})
        session_id = uuid.UUID({_python_literal(session_id)})
        work_id = uuid.UUID({_python_literal(work_id)})
        if not AgentSession.objects.filter(
            id=session_id,
            organization_id=organization_id,
            project_id=project_id,
        ).exists():
            raise RuntimeError('isolated CP3 session no longer exists')
        if not WorkflowWork.objects.filter(
            id=work_id,
            organization_id=organization_id,
            project_id=project_id,
        ).exists():
            raise RuntimeError('isolated CP3 root work no longer exists')
        result = retry_failed_distillations()
        print(json.dumps({{
            'session_queued': result['reconciled'],
            'candidate_queued': result['candidate_reconciled'],
        }}, sort_keys=True))
        """
    ).strip()


def expire_active_lease_code(scope: ScopeIds) -> str:
    organization_id = _uuid_text(scope.organization_id, "organization_id")
    project_id = _uuid_text(scope.project_id, "project_id")
    work_id = _uuid_text(scope.work_id, "work_id")

    return dedent(
        f"""
        import json
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        from engram.core.models import WorkflowRun, WorkflowWork

        organization_id = uuid.UUID({_python_literal(organization_id)})
        project_id = uuid.UUID({_python_literal(project_id)})
        work_id = uuid.UUID({_python_literal(work_id)})
        expired_at = timezone.now() - timedelta(seconds=1)
        heartbeat_at = expired_at - timedelta(seconds=1)
        work_updated = WorkflowWork.objects.filter(
            id=work_id,
            organization_id=organization_id,
            project_id=project_id,
            execution_state='leased',
        ).update(heartbeat_at=heartbeat_at, lease_expires_at=expired_at)
        run_updated = WorkflowRun.objects.filter(
            work_id=work_id,
            execution_contract_version=1,
            status='running',
        ).update(heartbeat_at=heartbeat_at, lease_expires_at=expired_at)
        print(json.dumps({{'work_updated': work_updated, 'run_updated': run_updated}}, sort_keys=True))
        """
    ).strip()


def _last_json_object(output: str, label: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise HarnessError(f"{label} did not emit a JSON object")


def _scope_from_seed(payload: dict[str, object]) -> ScopeIds:
    values: dict[str, str] = {}
    for field in ("organization_id", "project_id", "team_id", "session_id", "work_id"):
        value = payload.get(field)
        if not isinstance(value, str):
            raise HarnessError(f"Seed output is missing {field}")
        values[field] = _uuid_text(value, field)

    return ScopeIds(**values)


class Harness:
    def __init__(
        self,
        *,
        project: str,
        compose_file: Path,
        env_file: Path,
        override_file: Path,
        generated_secrets: Sequence[str],
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        deadline: Deadline | None = None,
    ) -> None:
        validate_project_name(project)
        _validate_absolute_canonical(compose_file, "Compose file")
        _validate_absolute_canonical(env_file, "generated env file")
        _validate_absolute_canonical(override_file, "generated Compose override")
        self.project = project
        self.compose_file = compose_file
        self.env_file = env_file
        self.override_file = override_file
        self.generated_secrets = tuple(generated_secrets)
        self.runner = runner
        self.deadline = deadline or Deadline(
            WORKLOAD_TIMEOUT, label="workload deadline"
        )

    @property
    def compose_prefix(self) -> list[str]:
        return compose_prefix(
            project=self.project,
            compose_file=self.compose_file,
            env_file=self.env_file,
            override_file=self.override_file,
        )

    @property
    def command_env(self) -> dict[str, str]:
        return deterministic_env(self.env_file)

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float = COMMAND_TIMEOUT,
        check: bool = True,
    ) -> CommandResult:
        effective_timeout = min(timeout, self.deadline.remaining())
        try:
            completed = self.runner(
                list(args),
                cwd=ROOT,
                env=self.command_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as error:
            command = redact_diagnostics(" ".join(args), self.generated_secrets)
            raise HarnessError(
                f"Command timed out after {effective_timeout:.1f}s: {command}"
            ) from error
        result = CommandResult(
            args=tuple(str(value) for value in args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            command = redact_diagnostics(" ".join(result.args), self.generated_secrets)
            stdout = redact_diagnostics(result.stdout, self.generated_secrets)
            stderr = redact_diagnostics(result.stderr, self.generated_secrets)
            raise CommandFailure(
                result.returncode,
                f"Command failed ({result.returncode}): {command}\nstdout tail:\n{stdout}\nstderr tail:\n{stderr}",
            )

        return result

    def compose(
        self, *args: str, timeout: float = COMMAND_TIMEOUT, check: bool = True
    ) -> CommandResult:
        return self.run([*self.compose_prefix, *args], timeout=timeout, check=check)

    def assert_project_absent(self) -> None:
        filters = (
            ("container", ["docker", "ps", "-aq"]),
            ("network", ["docker", "network", "ls", "-q"]),
            ("volume", ["docker", "volume", "ls", "-q"]),
        )
        for label, prefix in filters:
            result = self.run(
                [
                    *prefix,
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                ],
                timeout=30,
            )
            if result.stdout.strip():
                raise HarnessError(
                    f"Refusing to reuse existing {label} resources for {self.project}"
                )

    def api_shell_json(
        self, code: str, *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        result = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "shell",
            "-c",
            code,
            timeout=timeout,
        )

        return _last_json_object(result.stdout, "Django shell command")

    def query_state(
        self, scope: ScopeIds, *, timeout: float = COMMAND_TIMEOUT
    ) -> DistillationState:
        result = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "shell",
            "-c",
            state_query_code(scope),
            timeout=timeout,
        )
        lines = result.stdout.splitlines()
        if not lines:
            raise HarnessError("State query produced no output")
        for line in reversed(lines):
            try:
                return parse_state(line)
            except HarnessError:
                continue

        raise HarnessError("State query did not emit the strict CP3 state object")

    def reconcile_once(
        self, scope: ScopeIds, *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        return self.api_shell_json(reconcile_code(scope), timeout=timeout)

    def expire_active_lease(self, scope: ScopeIds) -> None:
        result = self.api_shell_json(expire_active_lease_code(scope))
        if result != {"run_updated": 1, "work_updated": 1}:
            raise HarnessError(
                f"Worker-loss fault did not expire one active claim: {result!r}"
            )

    def switch_provider(self, mode: str) -> None:
        write_env_file(self.env_file, provider_mode=mode)
        write_override_file(self.override_file, self.project)
        self.compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "worker-batch",
            timeout=120,
        )

    def dump_failure_logs(self) -> str:
        result = self.compose(
            "logs",
            "--no-color",
            "--tail=120",
            "api",
            "worker-batch",
            "relay",
            timeout=30,
            check=False,
        )

        return redact_diagnostics(result.stdout + result.stderr, self.generated_secrets)


def reconcile_until_quiescent(
    harness: ReconciliationHarness,
    scope: ScopeIds,
    *,
    deadline: Deadline,
    max_iterations: int = QUIESCENCE_ITERATIONS,
    sleeper: Callable[[float], None] = time.sleep,
) -> DistillationState:
    if type(max_iterations) is not int or max_iterations < 1:
        raise HarnessError("reconciliation iteration cap must be positive")
    for _iteration in range(max_iterations):
        remaining = deadline.remaining()
        harness.reconcile_once(scope, timeout=min(COMMAND_TIMEOUT, remaining))
        state = harness.query_state(
            scope, timeout=min(COMMAND_TIMEOUT, deadline.remaining())
        )
        if state_is_quiescent(state):
            return state
        sleeper(min(POLL_INTERVAL, deadline.remaining()))

    raise HarnessError(
        f"Distillation did not reach quiescence within {max_iterations} iterations"
    )


def _wait_for_state(
    harness: Harness,
    scope: ScopeIds,
    *,
    label: str,
    predicate: Callable[[DistillationState], bool],
    deadline: Deadline,
    max_iterations: int = FAULT_POLL_ITERATIONS,
) -> DistillationState:
    if max_iterations < 1:
        raise HarnessError("fault state iteration cap must be positive")
    last_state: DistillationState | None = None
    for _iteration in range(max_iterations):
        last_state = harness.query_state(
            scope, timeout=min(COMMAND_TIMEOUT, deadline.remaining())
        )
        if predicate(last_state):
            return last_state
        time.sleep(min(POLL_INTERVAL, deadline.remaining()))

    summary = (
        f"attempts={last_state.attempt_count} active={last_state.active_attempt_count} "
        f"extract={last_state.extract_complete_target_count} provider_failures="
        f"{last_state.provider_transient_attempt_count}"
        if last_state is not None
        else "no state"
    )
    raise HarnessError(
        f"Timed out waiting for {label} within {max_iterations} iterations: {summary}"
    )


def preserved_exit_code(
    primary_error: HarnessError | None, cleanup_returncode: int
) -> int:
    if isinstance(primary_error, CommandFailure):
        return primary_error.returncode
    if primary_error is not None:
        return 1
    if cleanup_returncode != 0:
        return cleanup_returncode

    return 0


def _perform_cleanup(
    *,
    project: str,
    env_file: Path,
    override_file: Path,
    generated_secrets: Sequence[str],
) -> CommandResult:
    command = cleanup_command(
        project=project,
        compose_file=COMPOSE_FILE,
        env_file=env_file,
        override_file=override_file,
    )
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=deterministic_env(env_file),
            text=True,
            capture_output=True,
            check=False,
            timeout=CLEANUP_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError(f"Exact project cleanup timed out for {project}") from error
    result = CommandResult(
        tuple(command), completed.returncode, completed.stdout, completed.stderr
    )
    if result.returncode != 0:
        diagnostic = redact_diagnostics(
            result.stdout + result.stderr, generated_secrets
        )
        progress(f"cleanup failed ({result.returncode}): {diagnostic}")

    return result


def _bootstrap_scope(
    harness: Harness, api_key: str, agent_key: str, run_id: str
) -> ScopeIds:
    bootstrap = _last_json_object(
        harness.compose(
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "engram_bootstrap_golden_path",
            "--api-key",
            api_key,
            "--agent-key",
            agent_key,
            "--provider-base-url",
            "http://127.0.0.1:1",
            "--json",
        ).stdout,
        "golden path bootstrap",
    )
    required = {}
    for field in ("organization_id", "project_id", "team_id"):
        value = bootstrap.get(field)
        if not isinstance(value, str):
            raise HarnessError(f"Bootstrap output is missing {field}")
        required[field] = _uuid_text(value, field)
    seeded = harness.api_shell_json(
        seed_code(
            run_id=run_id,
            organization_id=required["organization_id"],
            project_id=required["project_id"],
            team_id=required["team_id"],
        ),
        timeout=120,
    )

    return _scope_from_seed(seeded)


def run_live_fault_harness(
    harness: Harness,
    *,
    api_key: str,
    agent_key: str,
    run_id: str,
) -> DistillationState:
    harness.compose(
        "up",
        "-d",
        "--build",
        "--wait",
        "api",
        "worker-batch",
        "relay",
        timeout=STARTUP_TIMEOUT,
    )
    scope = _bootstrap_scope(harness, api_key, agent_key, run_id)
    accepted_deadline = Deadline(180.0, label="accepted-stage fault deadline")
    _wait_for_state(
        harness,
        scope,
        label="one accepted stage in an active attempt",
        predicate=worker_loss_fault_window,
        deadline=accepted_deadline,
    )
    harness.compose("kill", "-s", "SIGKILL", "worker-batch", timeout=30)
    harness.compose("stop", "-t", "2", "worker-batch", timeout=30)
    harness.expire_active_lease(scope)
    harness.reconcile_once(scope, timeout=60)

    harness.switch_provider("real")
    outage_deadline = Deadline(180.0, label="retryable provider-outage deadline")
    outage = _wait_for_state(
        harness,
        scope,
        label="retryable provider failure after worker loss",
        predicate=lambda state: (
            state.worker_lost_attempt_count >= 1
            and state.provider_transient_attempt_count >= 1
        ),
        deadline=outage_deadline,
    )
    if outage.root_disposition != "required":
        raise HarnessError("Provider outage unexpectedly resolved the root work")

    harness.switch_provider("fake")
    final = reconcile_until_quiescent(
        harness,
        scope,
        deadline=Deadline(900.0, label="bounded quiescence deadline"),
        max_iterations=QUIESCENCE_ITERATIONS,
    )
    assert_final_state(final)

    return final


def progress(message: str) -> None:
    print(f"[engram-cp3] {message}", flush=True)  # noqa: T201


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    if not COMPOSE_FILE.is_file():
        progress("failure: Compose contract is missing")

        return 1

    run_id = secrets.token_hex(8)
    api_key = f"egk_cp3_{secrets.token_urlsafe(32)}"
    agent_key = f"egk_cp3_agent_{secrets.token_urlsafe(32)}"
    generated_secrets = (api_key, agent_key)
    primary_error: HarnessError | None = None
    cleanup_returncode = 0
    owns_project = False
    with tempfile.TemporaryDirectory(prefix="engram-cp3-coverage-") as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        env_file = (temp_dir / "generated.env").resolve()
        override_file = (temp_dir / "override.yml").resolve()
        write_env_file(env_file, provider_mode="fake")
        write_override_file(
            override_file,
            arguments.project,
            fake_provider_delay_ms=FAULT_FAKE_PROVIDER_DELAY_MS,
        )
        harness = Harness(
            project=arguments.project,
            compose_file=COMPOSE_FILE,
            env_file=env_file,
            override_file=override_file,
            generated_secrets=generated_secrets,
        )
        progress(
            f"project={arguments.project} observations={EXPECTED_OBSERVATIONS} "
            f"provider_calls_per_attempt=2 deadline={GLOBAL_TIMEOUT:.0f}s"
        )
        try:
            harness.assert_project_absent()
            owns_project = True
            final = run_live_fault_harness(
                harness,
                api_key=api_key,
                agent_key=agent_key,
                run_id=run_id,
            )
            progress(
                "coverage evidence="
                + json.dumps(
                    {
                        "attempts": final.attempt_count,
                        "continuations": final.continuation_package_count,
                        "coverage": len(final.coverage_rows),
                        "extract_targets": final.extract_target_count,
                        "reduce_targets": final.reduce_target_count,
                        "P3": final.invariants["P3"].state,
                        "P5": final.invariants["P5"].state,
                    },
                    sort_keys=True,
                )
            )
        except HarnessError as error:
            primary_error = error
            progress(f"failure: {redact_diagnostics(str(error), generated_secrets)}")
            if owns_project:
                logs = harness.dump_failure_logs()
                if logs:
                    progress(f"failure logs tail:\n{logs}")
        finally:
            if owns_project:
                try:
                    cleanup = _perform_cleanup(
                        project=arguments.project,
                        env_file=env_file,
                        override_file=override_file,
                        generated_secrets=generated_secrets,
                    )
                    cleanup_returncode = cleanup.returncode
                except HarnessError as error:
                    cleanup_returncode = 1
                    progress(
                        f"cleanup failure: {redact_diagnostics(str(error), generated_secrets)}"
                    )
    exit_code = preserved_exit_code(primary_error, cleanup_returncode)
    if exit_code == 0:
        progress("disposable large-session distillation coverage passed")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from scripts import e2e_distillation_coverage as coverage


SAFE_PROJECT = "engram-cp3-coverage-0123456789abcdef"
RUN_ID = "fedcba9876543210"


def _observation_id(sequence: int) -> str:
    return f"00000000-0000-4000-8000-{sequence:012x}"


def _valid_payload() -> dict[str, object]:
    observations = [
        {
            "observation_id": _observation_id(sequence),
            "session_sequence": sequence,
        }
        for sequence in range(1, coverage.EXPECTED_OBSERVATIONS + 1)
    ]
    memberships = [
        {
            **observation,
            "chunk_ordinal": index,
        }
        for index, observation in enumerate(observations)
    ]
    coverage_rows = [
        {
            **observation,
            "outcome": "signal" if index % 2 == 0 else "no_signal",
            "source_count": 1 if index % 2 == 0 else 0,
            "deciding_stage_complete": True,
        }
        for index, observation in enumerate(observations)
    ]

    return {
        "root_work_count": 1,
        "window_count": 1,
        "root_disposition": "complete",
        "root_execution_state": "settled",
        "useful_observations": observations,
        "chunk_observation_counts": [1] * coverage.EXPECTED_OBSERVATIONS,
        "manifest_memberships": memberships,
        "attempt_count": 5,
        "continuation_package_count": 4,
        "active_attempt_count": 0,
        "worker_lost_attempt_count": 1,
        "provider_transient_attempt_count": 1,
        "extract_target_count": coverage.EXPECTED_OBSERVATIONS,
        "extract_complete_target_count": coverage.EXPECTED_OBSERVATIONS,
        "reduce_target_count": 7,
        "reduce_complete_target_count": 7,
        "pending_target_count": 0,
        "coverage": coverage_rows,
        "truncated_audit_count": 0,
        "candidate_generations": [
            {
                "candidate_id": "10000000-0000-4000-8000-000000000001",
                "content_hash": "a" * 64,
                "work_count": 1,
            }
        ],
        "pending_outbox_count": 0,
        "invariants": {
            "P3": {
                "state": "healthy",
                "reason": "latest_distillation_window_complete",
            },
            "P5": {
                "state": "healthy",
                "reason": "completed_window_observations_disposed",
            },
        },
    }


def _state(payload: dict[str, object] | None = None) -> coverage.DistillationState:
    return coverage.parse_state(json.dumps(payload or _valid_payload()))


def test_parse_args_generates_an_anchored_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coverage.secrets, "token_hex", lambda count: "0123456789abcdef")

    arguments = coverage.parse_args([])

    assert arguments.project == SAFE_PROJECT


def test_parse_args_accepts_only_an_explicit_safe_project() -> None:
    assert coverage.parse_args(["--project", SAFE_PROJECT]).project == SAFE_PROJECT
    with pytest.raises(
        coverage.HarnessError, match="unsafe disposable Compose project"
    ):
        coverage.parse_args(["--project", "engram-cp3-coverage-prod"])
    with pytest.raises(SystemExit) as captured:
        coverage.parse_args(["unexpected-positional"])
    assert captured.value.code == 2


@pytest.mark.parametrize(
    "project",
    [
        "engram-cp3-coverage-0123456789abcde",
        "engram-cp3-coverage-0123456789abcdef0",
        "ENGRAM-cp3-coverage-0123456789abcdef",
        "engram-cp3-coverage-0123456789abcdef-suffix",
        "other-0123456789abcdef",
        "",
    ],
)
def test_project_guard_is_fully_anchored(project: str) -> None:
    with pytest.raises(
        coverage.HarnessError, match="unsafe disposable Compose project"
    ):
        coverage.validate_project_name(project)


def test_compose_commands_are_deterministic_and_cleanup_is_exactly_project_scoped(
    tmp_path: Path,
) -> None:
    compose_file = (tmp_path / "compose.yml").resolve()
    env_file = (tmp_path / "generated.env").resolve()
    override_file = (tmp_path / "override.yml").resolve()
    expected_prefix = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        SAFE_PROJECT,
        "-f",
        str(compose_file),
        "-f",
        str(override_file),
    ]

    assert (
        coverage.compose_prefix(
            project=SAFE_PROJECT,
            compose_file=compose_file,
            env_file=env_file,
            override_file=override_file,
        )
        == expected_prefix
    )
    assert coverage.cleanup_command(
        project=SAFE_PROJECT,
        compose_file=compose_file,
        env_file=env_file,
        override_file=override_file,
    ) == [*expected_prefix, "down", "-v", "--remove-orphans"]


def test_compose_command_rejects_noncanonical_paths(tmp_path: Path) -> None:
    with pytest.raises(coverage.HarnessError, match="absolute canonical path"):
        coverage.cleanup_command(
            project=SAFE_PROJECT,
            compose_file=Path("relative-compose.yml"),
            env_file=(tmp_path / "env").resolve(),
            override_file=(tmp_path / "override").resolve(),
        )


def test_command_environment_is_allowlisted_and_does_not_forward_host_secrets(
    tmp_path: Path,
) -> None:
    env_file = (tmp_path / "generated.env").resolve()

    result = coverage.deterministic_env(
        env_file,
        source={
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "DOCKER_HOST": "unix:///run/docker.sock",
            "HTTP_PROXY": "http://credential@proxy",
            "ENGRAM_PROVIDER_MODE": "host-secret-mode",
            "UNRELATED_SECRET": "must-not-leak",
        },
    )

    assert result == {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "LC_ALL": "C.UTF-8",
        "COMPOSE_ANSI": "never",
        "ENGRAM_ENV_FILE": str(env_file),
    }


def test_command_environment_preserves_windows_docker_runtime_paths(
    tmp_path: Path,
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    source = {
        "PATH": r"C:\Program Files\Docker\Docker\resources\bin",
        "SYSTEMROOT": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "USERPROFILE": r"C:\Users\runner",
        "APPDATA": r"C:\Users\runner\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\runner\AppData\Local",
    }

    result = coverage.deterministic_env(env_file, source=source)

    assert {name: result[name] for name in source} == source


def test_generated_env_forces_one_observation_chunks_two_calls_and_switchable_provider(
    tmp_path: Path,
) -> None:
    env_file = (tmp_path / "generated.env").resolve()

    coverage.write_env_file(env_file, provider_mode="fake")
    fake = env_file.read_text(encoding="utf-8")
    coverage.write_env_file(env_file, provider_mode="real")
    real = env_file.read_text(encoding="utf-8")

    assert "ENGRAM_DISTILL_CHUNK_CHAR_BUDGET=8000\n" in fake
    assert "ENGRAM_DISTILL_CHUNK_CHAR_CEILING=8000\n" in fake
    assert "ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT=2\n" in fake
    assert "ENGRAM_DISTILL_REDUCE_TARGET=1\n" in fake
    assert "ENGRAM_PROVIDER_HTTP_TIMEOUT=3\n" in fake
    assert "ENGRAM_PROVIDER_MODE=fake\n" in fake
    assert "PASSWORD" not in fake
    assert real == fake.replace(
        "ENGRAM_PROVIDER_MODE=fake\n", "ENGRAM_PROVIDER_MODE=real\n"
    )
    with pytest.raises(coverage.HarnessError, match="provider mode"):
        coverage.write_env_file(env_file, provider_mode="production")


def test_generated_override_uses_unique_image_and_ephemeral_loopback_port(
    tmp_path: Path,
) -> None:
    override_file = (tmp_path / "override.yml").resolve()

    coverage.write_override_file(
        override_file,
        SAFE_PROJECT,
        fake_provider_delay_ms=2500,
    )

    rendered = override_file.read_text(encoding="utf-8")
    assert rendered.count(f"image: {SAFE_PROJECT}-backend:cp3") == 3
    assert 'ENGRAM_FAKE_PROVIDER_DELAY_MS: "2500"' in rendered
    assert "127.0.0.1::8000" in rendered
    assert "8000:8000" not in rendered
    assert set(
        line.strip()
        for line in rendered.splitlines()
        if line.startswith("  ") and line.endswith(":")
    ) >= {
        "api:",
        "worker-batch:",
        "relay:",
    }
    with pytest.raises(coverage.HarnessError, match="fake provider delay"):
        coverage.write_override_file(
            override_file,
            SAFE_PROJECT,
            fake_provider_delay_ms=5001,
        )


def test_worker_loss_fault_window_requires_exactly_one_accepted_stage_and_active_attempt() -> (
    None
):
    payload = _valid_payload()
    payload["extract_complete_target_count"] = 1
    payload["reduce_complete_target_count"] = 0
    payload["active_attempt_count"] = 1

    assert coverage.worker_loss_fault_window(_state(payload)) is True

    payload["extract_complete_target_count"] = 2
    assert coverage.worker_loss_fault_window(_state(payload)) is False


def test_seed_and_state_queries_are_deterministic_and_scope_bound() -> None:
    scope = coverage.ScopeIds(
        organization_id="20000000-0000-4000-8000-000000000001",
        project_id="20000000-0000-4000-8000-000000000002",
        team_id="20000000-0000-4000-8000-000000000003",
        session_id="20000000-0000-4000-8000-000000000004",
        work_id="20000000-0000-4000-8000-000000000005",
    )
    seed = coverage.seed_code(
        run_id=RUN_ID,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        team_id=scope.team_id,
    )

    assert seed == coverage.seed_code(
        run_id=RUN_ID,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        team_id=scope.team_id,
    )
    assert "range(1, 102)" in seed
    assert "'x' * 8500" in seed
    assert scope.organization_id in seed
    assert scope.project_id in seed
    state_query = coverage.state_query_code(scope)
    assert state_query == coverage.state_query_code(scope)
    assert "subject_type='agent_session'" in state_query
    assert "root = roots.filter(id=work_id).first()" in state_query
    assert "window = windows.filter(work_id=work_id).first()" in state_query
    assert coverage.reconcile_code(scope) == coverage.reconcile_code(scope)
    generated_programs = (
        seed,
        state_query,
        coverage.reconcile_code(scope),
        coverage.expire_active_lease_code(scope),
    )
    for code in generated_programs:
        compile(code, "<generated CP3 E2E program>", "exec")
        assert scope.organization_id in code
        assert scope.project_id in code
        assert "API key" not in code
    for code in generated_programs[1:3]:
        assert scope.session_id in code
    reconcile = coverage.reconcile_code(scope)
    assert "retry_failed_distillations" in reconcile
    assert "register_default_candidate_decision_builder" not in reconcile
    assert "reconcile_candidate_work(" not in reconcile
    with pytest.raises(coverage.HarnessError, match="unsafe run id"):
        coverage.seed_code(
            run_id="unsafe; import os",
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            team_id=scope.team_id,
        )


def test_state_parser_accepts_the_exact_schema() -> None:
    state = _state()

    assert state.root_work_count == 1
    assert len(state.useful_observations) == coverage.EXPECTED_OBSERVATIONS
    assert len(state.manifest_memberships) == coverage.EXPECTED_OBSERVATIONS
    assert len(state.coverage_rows) == coverage.EXPECTED_OBSERVATIONS
    assert state.invariants["P3"].state == "healthy"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not-json", "state output is not valid JSON"),
        ("[]", "state output must be a JSON object"),
        (json.dumps({**_valid_payload(), "extra": True}), "state object keys"),
        (
            json.dumps(
                {
                    key: value
                    for key, value in _valid_payload().items()
                    if key != "window_count"
                }
            ),
            "state object keys",
        ),
        (
            json.dumps({**_valid_payload(), "attempt_count": True}),
            "attempt_count must be an integer",
        ),
    ],
)
def test_state_parser_fails_closed_on_malformed_top_level(
    payload: str, message: str
) -> None:
    with pytest.raises(coverage.HarnessError, match=message):
        coverage.parse_state(payload)


def test_state_parser_rejects_malformed_nested_rows() -> None:
    payload = _valid_payload()
    payload["coverage"][0].pop("source_count")  # type: ignore[index,union-attr]

    with pytest.raises(coverage.HarnessError, match="coverage row keys"):
        coverage.parse_state(json.dumps(payload))


def _set(payload: dict[str, object], key: str, value: object) -> None:
    payload[key] = value


def _drop_useful_observation(payload: dict[str, object]) -> None:
    payload["useful_observations"].pop()  # type: ignore[union-attr]


def _break_chunk_size(payload: dict[str, object]) -> None:
    payload["chunk_observation_counts"][0] = 2  # type: ignore[index]


def _duplicate_manifest_sequence(payload: dict[str, object]) -> None:
    payload["manifest_memberships"][1]["session_sequence"] = 1  # type: ignore[index]


def _remove_reduce_targets(payload: dict[str, object]) -> None:
    payload["reduce_target_count"] = 0
    payload["reduce_complete_target_count"] = 0


def _drop_coverage(payload: dict[str, object]) -> None:
    payload["coverage"].pop()  # type: ignore[union-attr]


def _duplicate_coverage_sequence(payload: dict[str, object]) -> None:
    payload["coverage"][1]["session_sequence"] = 1  # type: ignore[index]


def _break_deciding_stage(payload: dict[str, object]) -> None:
    payload["coverage"][0]["deciding_stage_complete"] = False  # type: ignore[index]


def _break_signal_source(payload: dict[str, object]) -> None:
    payload["coverage"][0]["source_count"] = 0  # type: ignore[index]


def _break_no_signal_source(payload: dict[str, object]) -> None:
    payload["coverage"][1]["source_count"] = 1  # type: ignore[index]


def _break_coverage_outcome(payload: dict[str, object]) -> None:
    payload["coverage"][0]["outcome"] = "unknown"  # type: ignore[index]


def _duplicate_candidate_generation(payload: dict[str, object]) -> None:
    payload["candidate_generations"][0]["work_count"] = 2  # type: ignore[index]


def _break_invariant(payload: dict[str, object], invariant_id: str) -> None:
    payload["invariants"][invariant_id]["state"] = "violated"  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: _set(value, "root_work_count", 0), "exactly one root work"),
        (lambda value: _set(value, "window_count", 0), "exactly one window"),
        (_drop_useful_observation, "101 useful observations"),
        (_break_chunk_size, "one observation per chunk"),
        (_duplicate_manifest_sequence, "manifest membership"),
        (lambda value: _set(value, "attempt_count", 1), "more than one root attempt"),
        (
            lambda value: _set(value, "continuation_package_count", 0),
            "continuation package history",
        ),
        (
            lambda value: _set(value, "worker_lost_attempt_count", 0),
            "worker-loss attempt evidence",
        ),
        (
            lambda value: _set(value, "provider_transient_attempt_count", 0),
            "provider-outage attempt evidence",
        ),
        (
            lambda value: _set(value, "extract_complete_target_count", 100),
            "extraction targets are incomplete",
        ),
        (_remove_reduce_targets, "reduction targets were not materialized"),
        (
            lambda value: _set(value, "reduce_complete_target_count", 6),
            "reduction targets are incomplete",
        ),
        (
            lambda value: _set(value, "pending_target_count", 1),
            "provider targets remain pending",
        ),
        (_drop_coverage, "exactly 101 coverage rows"),
        (_duplicate_coverage_sequence, "coverage rows do not match"),
        (_break_deciding_stage, "coverage deciding stage is incomplete"),
        (_break_signal_source, "signal coverage must have at least one source"),
        (_break_no_signal_source, "no-signal coverage must not have a source"),
        (_break_coverage_outcome, "Coverage outcome is invalid"),
        (
            lambda value: _set(value, "truncated_audit_count", 1),
            "SessionDistillationTruncated",
        ),
        (
            lambda value: _set(value, "candidate_generations", []),
            "candidate decision generation",
        ),
        (
            _duplicate_candidate_generation,
            "exactly one current candidate-decision work",
        ),
        (lambda value: _break_invariant(value, "P3"), "P3 is not healthy"),
        (lambda value: _break_invariant(value, "P5"), "P5 is not healthy"),
        (
            lambda value: _set(value, "root_disposition", "required"),
            "root work is not settled",
        ),
        (
            lambda value: _set(value, "active_attempt_count", 1),
            "active root attempts remain",
        ),
        (
            lambda value: _set(value, "pending_outbox_count", 1),
            "distillation delivery remains pending",
        ),
    ],
)
def test_final_assertions_fail_closed_for_each_acceptance_branch(
    mutator: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    payload = copy.deepcopy(_valid_payload())
    mutator(payload)

    with pytest.raises(coverage.HarnessError, match=message):
        coverage.assert_final_state(_state(payload))


def test_final_assertions_accept_the_complete_isolated_state() -> None:
    coverage.assert_final_state(_state())


def test_final_assertions_accept_multiple_sources_for_one_signal_observation() -> None:
    payload = _valid_payload()
    payload["coverage"][0]["source_count"] = 2  # type: ignore[index]

    coverage.assert_final_state(_state(payload))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("root_disposition", "required"),
        ("root_execution_state", "leased"),
        ("active_attempt_count", 1),
        ("pending_target_count", 1),
        ("pending_outbox_count", 1),
    ],
)
def test_quiescence_requires_terminal_work_no_active_delivery_and_no_pending_target(
    key: str, value: object
) -> None:
    payload = _valid_payload()
    payload[key] = value

    assert coverage.state_is_quiescent(_state(payload)) is False


def test_deadline_and_iteration_caps_are_explicit() -> None:
    now = [10.0]
    deadline = coverage.Deadline(5.0, clock=lambda: now[0], label="test deadline")
    assert deadline.remaining() == 5.0
    now[0] = 15.0
    with pytest.raises(coverage.HarnessError, match="test deadline"):
        deadline.remaining()

    class NeverQuiescentHarness:
        def __init__(self) -> None:
            self.reconciliations = 0

        def reconcile_once(
            self, scope: coverage.ScopeIds, *, timeout: float
        ) -> dict[str, object]:
            self.reconciliations += 1
            return {"session_queued": 0, "candidate_queued": 0}

        def query_state(
            self, scope: coverage.ScopeIds, *, timeout: float
        ) -> coverage.DistillationState:
            payload = _valid_payload()
            payload["root_disposition"] = "required"
            return _state(payload)

    harness = NeverQuiescentHarness()
    scope = coverage.ScopeIds("o", "p", "t", "s", "w")
    with pytest.raises(coverage.HarnessError, match="3 iterations"):
        coverage.reconcile_until_quiescent(  # type: ignore[arg-type]
            harness,
            scope,
            deadline=coverage.Deadline(30.0),
            max_iterations=3,
            sleeper=lambda _delay: None,
        )
    assert harness.reconciliations == 3


def test_reconciliation_stops_at_first_quiescent_state() -> None:
    states = []
    first = _valid_payload()
    first["root_disposition"] = "required"
    states.append(_state(first))
    states.append(_state())

    class EventuallyQuiescentHarness:
        def __init__(self) -> None:
            self.reconciliations = 0

        def reconcile_once(
            self, scope: coverage.ScopeIds, *, timeout: float
        ) -> dict[str, object]:
            self.reconciliations += 1
            return {"session_queued": 1, "candidate_queued": 0}

        def query_state(
            self, scope: coverage.ScopeIds, *, timeout: float
        ) -> coverage.DistillationState:
            return states.pop(0)

    harness = EventuallyQuiescentHarness()
    result = coverage.reconcile_until_quiescent(  # type: ignore[arg-type]
        harness,
        coverage.ScopeIds("o", "p", "t", "s", "w"),
        deadline=coverage.Deadline(30.0),
        max_iterations=5,
        sleeper=lambda _delay: None,
    )

    assert coverage.state_is_quiescent(result)
    assert harness.reconciliations == 2


def test_command_failure_preserves_exit_code_and_redacts_diagnostics(
    tmp_path: Path,
) -> None:
    secret = "egk_cp3_secret_value"

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            17,
            stdout=f"visible {secret}",
            stderr=f"sk-provider-secret {secret}",
        )

    harness = coverage.Harness(
        project=SAFE_PROJECT,
        compose_file=(tmp_path / "compose.yml").resolve(),
        env_file=(tmp_path / "env").resolve(),
        override_file=(tmp_path / "override.yml").resolve(),
        generated_secrets=[secret],
        runner=runner,
        deadline=coverage.Deadline(30.0),
    )

    with pytest.raises(coverage.CommandFailure) as captured:
        harness.run(["synthetic-command", secret])

    assert captured.value.returncode == 17
    assert secret not in str(captured.value)
    assert "sk-provider-secret" not in str(captured.value)
    assert str(captured.value).count("[REDACTED]") >= 2


@pytest.mark.parametrize(
    ("primary_error", "cleanup_returncode", "expected"),
    [
        (coverage.CommandFailure(17, "failed"), 9, 17),
        (coverage.HarnessError("assertion failed"), 9, 1),
        (None, 9, 9),
        (None, 0, 0),
    ],
)
def test_exit_code_selection_preserves_the_primary_failure(
    primary_error: coverage.HarnessError | None,
    cleanup_returncode: int,
    expected: int,
) -> None:
    assert coverage.preserved_exit_code(primary_error, cleanup_returncode) == expected


def test_redaction_is_bounded_and_removes_generated_and_provider_credentials() -> None:
    secret = "generated-admin-secret"
    value = f"prefix {secret} egk_disposable_credential sk-provider-secret " + (
        "x" * 5000
    )

    redacted = coverage.redact_diagnostics(value, [secret])

    assert secret not in redacted
    assert "egk_disposable_credential" not in redacted
    assert "sk-provider-secret" not in redacted
    assert len(redacted) <= coverage.OUTPUT_LIMIT

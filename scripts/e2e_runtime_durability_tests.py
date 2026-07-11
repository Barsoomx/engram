from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.e2e_runtime_durability as durability
from scripts.e2e_runtime_durability import (
    BEAT_RECREATE_ARGS,
    COMPOSE_FILE,
    EXPECTED_BEAT_ENTRIES,
    BeatSnapshot,
    CommandResult,
    Deadline,
    Harness,
    HarnessError,
    QueueState,
    RuntimeState,
    StopState,
    cleanup_command,
    deterministic_env,
    exact_state_query,
    parse_beat_snapshot,
    parse_pid_role,
    parse_queue_state,
    parse_runtime_state,
    parse_stop_state,
    read_beat_snapshot,
    redact_secrets,
    validate_project_name,
    write_port_override,
)


def test_project_guard_accepts_only_generated_fault_names() -> None:
    validate_project_name("engram-d1-fault-0123456789abcdef")

    for unsafe in (
        "engram",
        "engram-d1-fault",
        "engram-d1-fault-not-hex",
        "engram-d1-harness-tests",
        "default",
        "",
    ):
        with pytest.raises(HarnessError, match="disposable Compose project"):
            validate_project_name(unsafe)


def test_project_backend_images_are_unique_and_reject_unsafe_projects() -> None:
    first = durability.project_backend_image("engram-d1-fault-a1b2c3d4")
    second = durability.project_backend_image("engram-d1-fault-deadbeef")

    assert first == "engram-d1-fault-a1b2c3d4-backend:d1"
    assert second == "engram-d1-fault-deadbeef-backend:d1"
    assert first != second
    with pytest.raises(HarnessError, match="disposable Compose project"):
        durability.project_backend_image("engram")


def test_cleanup_is_scoped_to_absolute_contract_and_generated_env(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    env_file = tmp_path / "generated.env"
    compose_file.touch()
    env_file.touch()

    command = cleanup_command(
        project="engram-d1-fault-a1b2c3d4",
        compose_file=compose_file,
        env_file=env_file,
    )

    assert command == [
        "docker",
        "compose",
        "--env-file",
        str(env_file.resolve()),
        "-p",
        "engram-d1-fault-a1b2c3d4",
        "-f",
        str(compose_file.resolve()),
        "down",
        "-v",
        "--remove-orphans",
    ]


def test_cleanup_rejects_non_absolute_or_unsafe_scope(tmp_path: Path) -> None:
    absolute_env = tmp_path / "generated.env"
    absolute_env.touch()

    with pytest.raises(HarnessError, match="absolute"):
        cleanup_command(
            project="engram-d1-fault-a1b2c3d4",
            compose_file=tmp_path / ".." / "docker-compose.yml",
            env_file=absolute_env,
        )

    with pytest.raises(HarnessError, match="disposable Compose project"):
        cleanup_command(
            project="engram",
            compose_file=(tmp_path / "docker-compose.yml").resolve(),
            env_file=absolute_env.resolve(),
        )


def test_cleanup_removes_only_exact_project_image_tag_after_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "engram-d1-fault-a1b2c3d4"
    image = durability.project_backend_image(project)
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    commands: list[list[str]] = []
    image_present = [True]
    container_present = [True]

    def run_stub(args: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(args)
        if args[:3] == ["docker", "ps", "-aq"]:
            stdout = "container-1\n" if container_present[0] else ""

            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if args == [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            '{{index .Config.Labels "com.docker.compose.project"}}',
            "container-1",
        ]:
            return SimpleNamespace(returncode=0, stdout=f"{project}\n", stderr="")
        if args == ["docker", "rm", "-f", "container-1"]:
            container_present[0] = False

            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:4] == ["docker", "image", "ls", "--quiet"]:
            stdout = "sha256:shared-content\n" if image_present[0] else ""

            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if args == ["docker", "image", "rm", image]:
            image_present[0] = False

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(durability.subprocess, "run", run_stub)
    monkeypatch.setattr(durability.time, "sleep", lambda _: None)

    ok, output = durability.cleanup(project, env_file, [])

    assert ok is True
    assert '"images":0' in output
    image_rm_index = commands.index(["docker", "image", "rm", image])
    container_rm_index = commands.index(["docker", "rm", "-f", "container-1"])
    assert image_rm_index > container_rm_index
    assert ["docker", "image", "rm", image] in commands
    assert all("engram-backend:local" not in command for command in commands)
    assert all(
        "engram-d1-fault-deadbeef-backend:d1" not in command for command in commands
    )
    assert all("sha256:shared-content" not in command for command in commands)
    image_commands = [
        command for command in commands if command[:2] == ["docker", "image"]
    ]
    assert all(
        "prune" not in command and "-f" not in command for command in image_commands
    )


def test_cleanup_treats_missing_project_image_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "engram-d1-fault-a1b2c3d4"
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    commands: list[list[str]] = []

    def run_stub(args: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(args)

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(durability.subprocess, "run", run_stub)

    ok, output = durability.cleanup(project, env_file, [])

    assert ok is True
    assert '"images":0' in output
    assert not any(command[:3] == ["docker", "image", "rm"] for command in commands)


def test_cleanup_reports_exact_image_residual_when_tag_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "engram-d1-fault-a1b2c3d4"
    image = durability.project_backend_image(project)
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    commands: list[list[str]] = []
    now = [0.0]

    def clock() -> float:
        now[0] += 0.5

        return now[0]

    def run_stub(args: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(args)
        if args[:4] == ["docker", "image", "ls", "--quiet"]:
            return SimpleNamespace(
                returncode=0, stdout="sha256:shared-content\n", stderr=""
            )
        if args == ["docker", "image", "rm", image]:
            return SimpleNamespace(returncode=1, stdout="", stderr="in use")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(durability.subprocess, "run", run_stub)
    monkeypatch.setattr(durability.time, "monotonic", clock)
    monkeypatch.setattr(durability.time, "sleep", lambda _: None)

    ok, output = durability.cleanup(project, env_file, [])

    assert ok is False
    assert '"images": ["engram-d1-fault-a1b2c3d4-backend:d1"]' in output
    assert ["docker", "image", "rm", image] in commands
    assert all("engram-backend:local" not in command for command in commands)
    assert all("sha256:shared-content" not in command for command in commands)
    image_commands = [
        command for command in commands if command[:2] == ["docker", "image"]
    ]
    assert all(
        "prune" not in command and "-f" not in command for command in image_commands
    )


def test_generated_override_replaces_api_port_and_compose_prefix_uses_absolute_files(
    tmp_path: Path,
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    project = "engram-d1-fault-a1b2c3d4"
    write_port_override(override_file, project)
    harness = Harness(
        project=project,
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )

    backend_image = "engram-d1-fault-a1b2c3d4-backend:d1"
    assert override_file.read_text(encoding="utf-8") == (
        "services:\n"
        "  api:\n"
        f"    image: {backend_image}\n"
        "    ports: !override\n"
        '      - "127.0.0.1::8000"\n'
        "  worker-realtime:\n"
        f"    image: {backend_image}\n"
        "  worker-near-realtime:\n"
        f"    image: {backend_image}\n"
        "  worker-batch:\n"
        f"    image: {backend_image}\n"
        "  worker-highmemory:\n"
        f"    image: {backend_image}\n"
        "  worker-domain-events:\n"
        f"    image: {backend_image}\n"
        "  beat:\n"
        f"    image: {backend_image}\n"
        "  relay:\n"
        f"    image: {backend_image}\n"
    )
    assert "engram-backend:local" not in override_file.read_text(encoding="utf-8")
    assert harness.compose_prefix[-4:] == [
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(harness.override_file),
    ]
    assert all(
        Path(harness.compose_prefix[index + 1]).is_absolute()
        for index, item in enumerate(harness.compose_prefix)
        if item == "-f"
    )


def test_generated_override_rejects_unsafe_project(tmp_path: Path) -> None:
    override_file = (tmp_path / "ports.override.yml").resolve()

    with pytest.raises(HarnessError, match="disposable Compose project"):
        write_port_override(override_file, "engram")

    assert not override_file.exists()


def test_api_origin_resolver_refreshes_changed_loopback_mapping(tmp_path: Path) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    mappings = iter(("127.0.0.1:63779\n", "127.0.0.1:63796\n"))
    timeouts: list[float] = []

    def compose_stub(
        *args: str, timeout: float = 180.0, check: bool = True
    ) -> CommandResult:
        assert args == ("port", "api", "8000")
        timeouts.append(timeout)

        return CommandResult(args, 0, next(mappings), "")

    harness.compose = compose_stub  # type: ignore[method-assign]

    assert harness.refresh_api_origin(timeout=17.0) == "http://127.0.0.1:63779"
    assert harness.refresh_api_origin(timeout=11.0) == "http://127.0.0.1:63796"
    assert harness.server_url == "http://127.0.0.1:63796"
    assert timeouts == pytest.approx([17.0, 11.0])


@pytest.mark.parametrize(
    "mapping",
    (
        "",
        "0.0.0.0:1234\n",
        "[::1]:1234\n",
        "localhost:1234\n",
        "127.0.0.1\n",
        "127.0.0.1:0\n",
        "127.0.0.1:65536\n",
        "127.0.0.1:0123\n",
        " 127.0.0.1:1234\n",
        "127.0.0.1:1234 \n",
        "127.0.0.1:1234\n127.0.0.1:5678\n",
    ),
)
def test_api_origin_resolver_rejects_noncanonical_mappings(
    tmp_path: Path, mapping: str
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    harness.compose = lambda *args, **kwargs: CommandResult(args, 0, mapping, "")  # type: ignore[method-assign]

    with pytest.raises(HarnessError, match="loopback API port mapping"):
        harness.refresh_api_origin()


def test_fault_d_refreshes_api_origin_and_reconnects_before_relay_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    api_deadlines: list[Deadline] = []
    api_timeouts: list[float] = []

    class FakeHarness:
        server_url = "http://127.0.0.1:63779"

        def compose(self, *args: str, timeout: float) -> CommandResult:
            if args == ("start", "api"):
                events.append(("start", self.server_url))
                api_timeouts.append(timeout)

            return CommandResult(args, 0, "", "")

        def refresh_api_origin(self, *, deadline: Deadline) -> str:
            api_deadlines.append(deadline)
            api_timeouts.append(deadline.remaining())
            self.server_url = "http://127.0.0.1:63796"
            events.append(("refresh", self.server_url))

            return self.server_url

        def wait_role(
            self,
            service: str,
            timeout: float = 90.0,
            *,
            deadline: Deadline | None = None,
        ) -> str:
            if service == "api" and deadline is not None:
                api_deadlines.append(deadline)
                events.append(("role", self.server_url))

            return "granian"

        def stop_service(self, service: str, timeout: float) -> tuple[StopState, float]:
            return StopState(False, 0, False, "2026-07-11T00:00:01Z"), 0.1

        def submit_hook(
            self, config_dir: Path, run_id: str, *, timeout: float
        ) -> dict[str, object]:
            events.append(("hook", self.server_url))

            return {"status": "accepted"}

        def assert_queue(
            self, *, ready: int, unacknowledged: int, timeout: float
        ) -> None:
            return None

    monkeypatch.setattr(
        durability,
        "_assert_service_registration",
        lambda harness, service, timeout: (
            (
                events.append(("health", harness.server_url)),
                api_timeouts.append(timeout),
            )
            if service == "api"
            else None
        ),
    )
    monkeypatch.setattr(
        durability,
        "connect_cli",
        lambda harness, config_dir, api_key, bootstrap, deadline: (
            events.append(("connect", harness.server_url))
            or api_deadlines.append(deadline)
            or api_timeouts.append(deadline.remaining())
        ),
    )
    monkeypatch.setattr(durability, "assert_fault_baseline", lambda *args: None)
    monkeypatch.setattr(durability, "wait_exact", lambda *args, **kwargs: {})
    monkeypatch.setattr(durability, "wait_queue", lambda *args, **kwargs: None)
    monkeypatch.setattr(durability, "progress", lambda message: None)

    durability.fault_d(
        FakeHarness(),  # type: ignore[arg-type]
        Path("/tmp/config"),
        "project",
        "relay-run",
        api_key="api-key",
        bootstrap={"project_id": "project", "team_id": "team"},
    )

    assert [name for name, _ in events] == [
        "start",
        "role",
        "health",
        "refresh",
        "connect",
        "hook",
    ]
    assert events[3][1] == "http://127.0.0.1:63796"
    assert events[4][1] == "http://127.0.0.1:63796"
    assert len({id(deadline) for deadline in api_deadlines}) == 1
    assert all(
        later < earlier for earlier, later in zip(api_timeouts, api_timeouts[1:])
    )


def test_hook_result_error_includes_bounded_redacted_stderr() -> None:
    secret = "top-secret-token"
    overlapping = "top-secret"
    result = CommandResult(
        args=("must", "not", "appear"),
        returncode=0,
        stdout="",
        stderr="discarded-prefix"
        + ("x" * 5000)
        + f" code=offline detail={secret} {overlapping}",
    )

    with pytest.raises(HarnessError) as captured:
        durability.parse_hook_result(  # type: ignore[attr-defined]
            result,
            [overlapping, secret],
        )

    message = str(captured.value)
    assert "returncode=0" in message
    assert "code=offline" in message
    assert "detail=[REDACTED]" in message
    assert secret not in message
    assert overlapping not in message
    assert "discarded-prefix" not in message
    assert "must not appear" not in message
    assert len(message) < 4300


def test_hook_result_returns_successful_object_unchanged() -> None:
    result = CommandResult(
        args=(),
        returncode=0,
        stdout='noise\n{"status":"accepted","observation_id":"observation-1"}\n',
        stderr="benign warning",
    )

    assert durability.parse_hook_result(result, []) == {  # type: ignore[attr-defined]
        "status": "accepted",
        "observation_id": "observation-1",
    }


def test_container_identity_lookup_includes_stopped_containers(tmp_path: Path) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    calls: list[tuple[str, ...]] = []

    def compose_stub(
        *args: str, timeout: float = 180.0, check: bool = True
    ) -> CommandResult:
        calls.append(args)

        return CommandResult(
            args=args, returncode=0, stdout="container-id\n", stderr=""
        )

    harness.compose = compose_stub  # type: ignore[method-assign]

    assert harness.container_id("beat") == "container-id"
    assert calls == [("ps", "-a", "-q", "beat")]


def test_selected_mount_parser_ignores_docker_format_trailing_blank_line(
    tmp_path: Path,
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    harness.container_id = lambda service, timeout=180: "container-id"  # type: ignore[method-assign]
    harness.inspect_selected = (  # type: ignore[method-assign]
        lambda container_id, template, timeout=180: (
            '"volume"\t"beat-volume"\t"/beat"\n\n'
        )
    )

    assert harness.volume_name("beat", "/beat") == "beat-volume"


def test_beat_recreation_command_creates_without_starting_or_dependencies() -> None:
    assert BEAT_RECREATE_ARGS == (
        "up",
        "--no-start",
        "--no-build",
        "--no-deps",
        "--force-recreate",
        "beat",
    )


def test_redaction_handles_multiple_overlapping_secrets_longest_first() -> None:
    value = "token=abc123; short=abc; repeated=abc123"

    assert redact_secrets(value, ["abc", "abc123", ""]) == (
        "token=[REDACTED]; short=[REDACTED]; repeated=[REDACTED]"
    )


def test_redaction_removes_disposable_bootstrap_credentials_from_logs() -> None:
    value = "Generated admin password: random-password\nAPI key (shown once):\nengram-default-random-key\n"

    redacted = redact_secrets(value, [])

    assert "random-password" not in redacted
    assert "engram-default-random-key" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_deadline_uses_injected_monotonic_clock() -> None:
    now = [10.0]
    deadline = Deadline(25.0, clock=lambda: now[0])

    assert deadline.remaining() == 25.0
    now[0] = 34.5
    assert deadline.remaining() == 0.5
    now[0] = 35.0
    with pytest.raises(HarnessError, match="global deadline"):
        deadline.remaining()


def test_controller_budget_reserves_bounded_failure_and_cleanup_tail() -> None:
    failure_log_max = (
        len(durability.FAILURE_LOG_SERVICES) * durability.FAILURE_LOG_TIMEOUT
    )
    cleanup_max = (
        durability.CLEANUP_COMPOSE_TIMEOUT + durability.CLEANUP_FALLBACK_TIMEOUT
    )

    assert failure_log_max + cleanup_max <= durability.POST_WORKLOAD_RESERVE


def test_workload_and_reserved_tail_equal_global_controller_budget() -> None:
    assert (
        durability.WORKLOAD_TIMEOUT + durability.POST_WORKLOAD_RESERVE
        == durability.GLOBAL_TIMEOUT
    )


def test_harness_uses_workload_budget_for_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    captured_deadlines: list[tuple[float, str]] = []
    sentinel = object()

    def deadline_stub(timeout: float, *, label: str) -> object:
        captured_deadlines.append((timeout, label))

        return sentinel

    monkeypatch.setattr(durability, "Deadline", deadline_stub)

    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )

    assert captured_deadlines == [(durability.WORKLOAD_TIMEOUT, "workload deadline")]
    assert harness.deadline is sentinel


def test_named_deadline_shares_one_absolute_budget() -> None:
    now = [5.0]
    deadline = Deadline(10.0, clock=lambda: now[0], label="Fault A relay")

    now[0] = 9.0
    assert deadline.remaining() == 6.0
    now[0] = 15.0
    with pytest.raises(HarnessError, match="Fault A relay"):
        deadline.remaining()


def test_beat_snapshot_helper_shares_deadline_across_create_and_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [20.0]
    deadline = Deadline(10.0, clock=lambda: now[0], label="Beat helper")
    timeouts: list[float] = []

    class FakeHarness:
        project = "engram-d1-fault-a1b2c3d4"
        command_env: dict[str, str] = {}

        def run(self, args: list[str], *, timeout: float) -> CommandResult:
            timeouts.append(timeout)
            now[0] += 4.0
            entries = {
                name: {"last_run_at": None, "total_run_count": 0}
                for name in EXPECTED_BEAT_ENTRIES
            }

            return CommandResult(args, 0, json.dumps({"entries": entries}), "")

    cleanup_timeouts: list[float] = []

    def cleanup_stub(*args: object, timeout: float, **kwargs: object) -> object:
        cleanup_timeouts.append(timeout)

        return object()

    monkeypatch.setattr("scripts.e2e_runtime_durability.subprocess.run", cleanup_stub)

    snapshot = read_beat_snapshot(  # type: ignore[arg-type]
        FakeHarness(),
        "beat-volume",
        "sha256:image",
        deadline=deadline,
    )

    assert len(snapshot.entries) == len(EXPECTED_BEAT_ENTRIES)
    assert timeouts == [10.0, 6.0]
    assert cleanup_timeouts == [2.0]


def test_compose_environment_is_allowlisted_and_generated(tmp_path: Path) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env = deterministic_env(
        env_file,
        source={
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "DOCKER_HOST": "unix:///run/docker.sock",
            "ENGRAM_PROVIDER_MODE": "host-secret-mode",
            "HTTP_PROXY": "http://credential@proxy",
            "UNRELATED_SECRET": "must-not-leak",
        },
    )

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "LC_ALL": "C.UTF-8",
        "ENGRAM_ENV_FILE": str(env_file),
        "ENGRAM_PROVIDER_MODE": "fake",
        "ENGRAM_RABBITMQ_HOSTNAME": "rabbitmq",
        "ENGRAM_RABBITMQ_NODENAME": "rabbit@rabbitmq",
    }


def test_poll_passes_each_probe_its_remaining_phase_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    write_port_override(override_file, "engram-d1-fault-a1b2c3d4")
    harness = Harness(
        project="engram-d1-fault-a1b2c3d4",
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    now = [100.0]
    monkeypatch.setattr("scripts.e2e_runtime_durability.time.monotonic", lambda: now[0])
    seen: list[float] = []

    def probe(remaining: float) -> object:
        seen.append(remaining)
        if len(seen) == 1:
            now[0] += 3.0
            raise HarnessError("retry")

        return "ready"

    monkeypatch.setattr("scripts.e2e_runtime_durability.time.sleep", lambda _: None)

    assert harness.poll("bounded probe", 10.0, probe) == "ready"
    assert seen == [10.0, 7.0]


def test_poll_propagates_workload_deadline_without_phase_timeout_wrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = (tmp_path / "generated.env").resolve()
    env_file.touch()
    override_file = (tmp_path / "ports.override.yml").resolve()
    project = "engram-d1-fault-a1b2c3d4"
    write_port_override(override_file, project)
    harness = Harness(
        project=project,
        env_file=env_file,
        override_file=override_file,
        generated_secrets=[],
    )
    now = [100.0]
    harness.deadline = Deadline(
        1.0,
        clock=lambda: now[0],
        label="workload deadline",
    )
    monkeypatch.setattr(durability.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(durability.time, "sleep", lambda _: None)

    def probe(remaining: float) -> object:
        now[0] += remaining + 0.1
        raise HarnessError("retryable phase probe failure")

    with pytest.raises(HarnessError) as captured:
        harness.poll("bounded probe", 10.0, probe)

    assert str(captured.value) == "Harness exceeded its workload deadline"


def test_queue_parser_requires_exactly_one_well_formed_target() -> None:
    state = parse_queue_state(
        json.dumps(
            [
                {
                    "name": "engram-near-realtime",
                    "messages_ready": 1,
                    "messages_unacknowledged": 0,
                }
            ]
        ),
        "engram-near-realtime",
    )

    assert state == QueueState(ready=1, unacknowledged=0)

    malformed_payloads = (
        "not-json",
        "{}",
        "[]",
        '[{"name":"other","messages_ready":0,"messages_unacknowledged":0}]',
        '[{"name":"engram-near-realtime","messages_ready":"1","messages_unacknowledged":0}]',
        '[{"name":"engram-near-realtime","messages_ready":true,"messages_unacknowledged":0}]',
        '[{"name":"engram-near-realtime","messages_ready":-1,"messages_unacknowledged":0}]',
        '[{"name":"engram-near-realtime","messages_ready":0,"messages_unacknowledged":false}]',
        '[{"name":"engram-near-realtime","messages_ready":0,"messages_unacknowledged":0},'
        '{"name":"engram-near-realtime","messages_ready":0,"messages_unacknowledged":0}]',
    )
    for payload in malformed_payloads:
        with pytest.raises(HarnessError, match="queue"):
            parse_queue_state(payload, "engram-near-realtime")


def test_runtime_state_parser_and_equality_cover_selected_fields_only() -> None:
    payload = json.dumps(
        {
            "container_id": "container-1",
            "pid": 412,
            "running": True,
            "started_at": "2026-07-11T00:00:00Z",
            "image_id": "sha256:image-1",
            "restart_count": 0,
        }
    )
    before = parse_runtime_state(payload)
    after = parse_runtime_state(payload)

    assert before == RuntimeState(
        container_id="container-1",
        pid=412,
        restart_count=0,
        started_at="2026-07-11T00:00:00Z",
        image_id="sha256:image-1",
    )
    assert before == after
    assert before != RuntimeState(
        "container-1", 413, 0, "2026-07-11T00:00:00Z", "sha256:image-1"
    )

    for invalid in (
        "not-json",
        "[]",
        "true",
        "{}",
        '{"container_id":"x","pid":true,"running":true,"restart_count":0,"started_at":"time","image_id":"i"}',
        '{"container_id":"x","pid":1,"running":true,"restart_count":-1,"started_at":"time","image_id":"i"}',
        "[]",
        '{"container_id":"x","pid":1,"running":false,"restart_count":0,"started_at":"time","image_id":"i"}',
    ):
        with pytest.raises(HarnessError, match="runtime state"):
            parse_runtime_state(invalid)


def test_stop_state_parser_rejects_running_oom_killed_and_malformed_fields() -> None:
    assert parse_stop_state(
        '{"running":false,"restarting":false,"exit_code":0,"oom_killed":false,"finished_at":"2026-07-11T00:00:01Z"}'
    ) == StopState(False, 0, False, "2026-07-11T00:00:01Z")

    for invalid in (
        "not-json",
        "[]",
        "{}",
        '{"running":true,"restarting":false,"exit_code":0,"oom_killed":false,"finished_at":"time"}',
        '{"running":false,"restarting":true,"exit_code":0,"oom_killed":false,"finished_at":"time"}',
        '{"running":false,"restarting":false,"exit_code":true,"oom_killed":false,"finished_at":"time"}',
        '{"running":false,"restarting":false,"exit_code":0,"oom_killed":true,"finished_at":"time"}',
        '{"running":false,"restarting":false,"exit_code":0,"oom_killed":false,"finished_at":"0001-01-01T00:00:00Z"}',
    ):
        with pytest.raises(HarnessError, match="stop state"):
            parse_stop_state(invalid)


def test_beat_snapshot_parser_requires_expected_subset_and_snapshots_all_entries() -> (
    None
):
    expected = frozenset({"daily-digest", "weekly-digest"})
    payload = json.dumps(
        {
            "entries": {
                "daily-digest": {
                    "last_run_at": "2026-07-11T00:00:00+00:00",
                    "total_run_count": 1,
                },
                "weekly-digest": {"last_run_at": None, "total_run_count": 0},
                "valid-extra": {"last_run_at": None, "total_run_count": 4},
            }
        }
    )

    assert parse_beat_snapshot(payload, expected) == BeatSnapshot(
        entries=(
            ("daily-digest", "2026-07-11T00:00:00+00:00", 1),
            ("valid-extra", None, 4),
            ("weekly-digest", None, 0),
        )
    )

    invalid = (
        "{}",
        "not-json",
        '{"entries":[]}',
        '{"entries":{"daily-digest":{"last_run_at":null,"total_run_count":0}}}',
        '{"entries":{"daily-digest":{"last_run_at":null,"total_run_count":0},'
        '"weekly-digest":{"last_run_at":null,"total_run_count":"0"}}}',
        '{"entries":{"daily-digest":{"last_run_at":false,"total_run_count":0},'
        '"weekly-digest":{"last_run_at":null,"total_run_count":0}}}',
        '{"entries":{"daily-digest":{"last_run_at":null,"total_run_count":-1},'
        '"weekly-digest":{"last_run_at":null,"total_run_count":0}}}',
        '{"entries":{"daily-digest":{"last_run_at":null,"total_run_count":true},'
        '"weekly-digest":{"last_run_at":null,"total_run_count":0}}}',
    )
    for payload in invalid:
        with pytest.raises(HarnessError, match="Beat snapshot"):
            parse_beat_snapshot(payload, expected)


@pytest.mark.parametrize(
    ("argv", "service", "expected"),
    (
        (
            ["granian", "--interface", "wsgi", "settings.wsgi:application"],
            "api",
            "granian",
        ),
        (
            [
                "/usr/local/bin/granian",
                "--interface",
                "wsgi",
                "settings.wsgi:application",
            ],
            "api",
            "granian",
        ),
        (
            [
                "python",
                "/usr/local/bin/granian",
                "--interface",
                "wsgi",
                "settings.wsgi:application",
            ],
            "api",
            "granian",
        ),
        (
            [
                "celery",
                "-A",
                "engram.celery_app",
                "worker",
                "-Q",
                "engram-near-realtime",
            ],
            "worker-near-realtime",
            "celery-worker",
        ),
        (
            [
                "python",
                "/usr/local/bin/celery",
                "-A",
                "engram.celery_app",
                "beat",
                "--schedule=/var/lib/engram-beat/celerybeat-schedule",
            ],
            "beat",
            "celery-beat",
        ),
        (["python", "manage.py", "celery_outbox_relay"], "relay", "outbox-relay"),
    ),
)
def test_pid_role_parser_accepts_console_script_and_python_shebang_forms(
    argv: list[str], service: str, expected: str
) -> None:
    assert parse_pid_role(argv, service) == expected


@pytest.mark.parametrize(
    ("argv", "service"),
    (
        (["sh", "-ec", "granian"], "api"),
        (["/bin/bash", "-c", "celery worker"], "worker-near-realtime"),
        (["celery", "-A", "engram.celery_app", "beat"], "worker-near-realtime"),
        (["python", "manage.py", "runserver"], "relay"),
        (["not-granian", "granian", "--interface", "wsgi"], "api"),
        (["granian", "--interface", "wsgi"], "api"),
        (["granian", "--interface", "wsgi", "other.wsgi:application"], "api"),
        (["python", "other.py", "manage.py", "celery_outbox_relay"], "relay"),
        (
            [
                "celery",
                "-A",
                "engram.celery_app",
                "worker",
                "--exclude-queues=engram-near-realtime",
            ],
            "worker-near-realtime",
        ),
        (
            [
                "celery",
                "-A",
                "engram.celery_app",
                "worker",
                "--queues",
                "engram-near-realtime",
            ],
            "worker-near-realtime",
        ),
        (
            [
                "celery",
                "-A",
                "engram.celery_app",
                "beat",
                "--schedule",
                "/not-the-beat-volume/x",
                "--schedule=/var/lib/engram-beat/celerybeat-schedule",
            ],
            "beat",
        ),
        (
            [
                "celery",
                "worker",
                "-Q",
                "engram-near-realtime",
                "-A",
                "engram.celery_app",
            ],
            "worker-near-realtime",
        ),
        ([], "api"),
    ),
)
def test_pid_role_parser_rejects_shells_and_wrong_roles(
    argv: list[str], service: str
) -> None:
    with pytest.raises(HarnessError, match="PID 1"):
        parse_pid_role(argv, service)


def test_pid_role_parser_requires_exact_beat_schedule_path() -> None:
    with pytest.raises(HarnessError, match="PID 1"):
        parse_pid_role(
            [
                "celery",
                "-A",
                "engram.celery_app",
                "beat",
                "--schedule=/tmp/celerybeat-schedule",
            ],
            "beat",
        )


def test_exact_state_query_requires_exact_golden_path_linkage() -> None:
    query = exact_state_query("project-1", "run-1")

    for required in (
        "MemoryStatus.APPROVED",
        "status=MemoryStatus.APPROVED",
        "version=F('memory__current_version')",
        "source_observation_ids",
        "'observations': observations.count()",
        "'memories': memories.count()",
        "'versions': versions.count()",
        "'documents': documents.count()",
        "'linked_documents': linked_documents",
    ):
        assert required in query

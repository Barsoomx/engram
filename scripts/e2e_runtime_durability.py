from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = (ROOT / "deploy/compose/docker-compose.yml").resolve()
ENV_EXAMPLE = (ROOT / "deploy/compose/.env.example").resolve()
TARGET_QUEUE = "engram-near-realtime"
TARGET_TASK = "engram.memory.process_observation_work_v1"
TARGET_CANDIDATE_DECISION_TASK = "engram.memory.process_candidate_decision_work_v1"
GLOBAL_TIMEOUT = 25 * 60.0
FAILURE_LOG_SERVICES = (
    "api",
    "rabbitmq",
    "relay",
    "worker-near-realtime",
    "beat",
)
FAILURE_LOG_TIMEOUT = 10.0
CLEANUP_COMPOSE_TIMEOUT = 120.0
CLEANUP_FALLBACK_TIMEOUT = 15.0
POST_WORKLOAD_RESERVE = 200.0
WORKLOAD_TIMEOUT = GLOBAL_TIMEOUT - POST_WORKLOAD_RESERVE
COMMAND_TIMEOUT = 180.0
STOP_CLI_OVERHEAD = 5.0
STOP_POST_INSPECT_TIMEOUT = 10.0
POLL_INTERVAL = 2.0
OUTPUT_LIMIT = 4000
PROJECT_PATTERN = re.compile(r"engram-d1-fault-[0-9a-f]{8,32}\Z")
EXPECTED_BEAT_ENTRIES = frozenset(
    {
        "daily-digest",
        "weekly-digest",
        "stale-session-sweep",
        "retry-failed-distillations",
        "reembed-missing-embeddings",
        "confidence-decay",
        "reconcile-candidate-decision-work",
        "expire-stale-import-jobs",
    }
)
BEAT_RECREATE_ARGS = (
    "up",
    "--no-start",
    "--no-build",
    "--no-deps",
    "--force-recreate",
    "beat",
)


class HarnessError(Exception):
    pass


@dataclass(frozen=True)
class CommandResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class QueueState:
    ready: int
    unacknowledged: int


@dataclass(frozen=True)
class RuntimeState:
    container_id: str
    pid: int
    restart_count: int
    started_at: str
    image_id: str


@dataclass(frozen=True)
class BeatSnapshot:
    entries: tuple[tuple[str, str | None, int], ...]


@dataclass(frozen=True)
class RabbitState:
    container_id: str
    nodename: str
    volume_name: str


@dataclass(frozen=True)
class StopState:
    running: bool
    exit_code: int
    oom_killed: bool
    finished_at: str


class Deadline:
    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        label: str = "global deadline",
    ) -> None:
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


def project_backend_image(project: str) -> str:
    validate_project_name(project)

    return f"{project}-backend:d1"


def _validate_absolute_canonical(path: Path, label: str) -> None:
    if not path.is_absolute() or path != path.resolve():
        raise HarnessError(f"{label} must be an absolute canonical path")


def cleanup_command(*, project: str, compose_file: Path, env_file: Path) -> list[str]:
    validate_project_name(project)
    _validate_absolute_canonical(compose_file, "Compose file")
    _validate_absolute_canonical(env_file, "generated env file")

    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        project,
        "-f",
        str(compose_file),
        "down",
        "-v",
        "--remove-orphans",
    ]


def deterministic_env(
    env_file: Path,
    *,
    source: Mapping[str, str] = os.environ,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    )
    env = {name: source[name] for name in allowed if source.get(name)}
    env.update(
        {
            "LC_ALL": "C.UTF-8",
            "ENGRAM_ENV_FILE": str(env_file),
            "ENGRAM_PROVIDER_MODE": "fake",
            "ENGRAM_RABBITMQ_HOSTNAME": "rabbitmq",
            "ENGRAM_RABBITMQ_NODENAME": "rabbit@rabbitmq",
        }
    )
    if extra:
        env.update(extra)

    return env


def write_port_override(path: Path, project: str) -> None:
    _validate_absolute_canonical(path, "generated Compose override")
    backend_image = project_backend_image(project)
    path.write_text(
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
        f"    image: {backend_image}\n",
        encoding="utf-8",
    )


def redact_secrets(value: str, generated_secrets: Sequence[str]) -> str:
    redacted = value
    for secret in sorted(
        {item for item in generated_secrets if item}, key=len, reverse=True
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?m)(Generated admin password:\s*)[^\r\n]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?<!\S)engram-default-\S+", "[REDACTED]", redacted)

    return redacted


def redact_tail(value: str, generated_secrets: Sequence[str]) -> str:
    return redact_secrets(value, generated_secrets)[-OUTPUT_LIMIT:]


def parse_queue_state(payload: str, queue_name: str) -> QueueState:
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HarnessError("Rabbit queue output is not valid JSON") from error
    if not isinstance(rows, list):
        raise HarnessError("Rabbit queue output must be a JSON list")
    matches = [
        row for row in rows if isinstance(row, dict) and row.get("name") == queue_name
    ]
    if len(matches) != 1:
        raise HarnessError(
            f"Expected exactly one target queue {queue_name!r}, got {len(matches)}"
        )
    ready = matches[0].get("messages_ready")
    unacknowledged = matches[0].get("messages_unacknowledged")
    if (
        not isinstance(ready, int)
        or isinstance(ready, bool)
        or ready < 0
        or not isinstance(unacknowledged, int)
        or isinstance(unacknowledged, bool)
        or unacknowledged < 0
    ):
        raise HarnessError("Rabbit queue counters must be non-negative integers")

    return QueueState(ready=ready, unacknowledged=unacknowledged)


def parse_runtime_state(payload: str) -> RuntimeState:
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HarnessError("Selected runtime state is not valid JSON") from error
    if not isinstance(rows, dict):
        raise HarnessError("Selected runtime state must be a JSON object")
    container_id = rows.get("container_id")
    pid = rows.get("pid")
    running = rows.get("running")
    restart_count = rows.get("restart_count")
    started_at = rows.get("started_at")
    image_id = rows.get("image_id")
    if (
        not isinstance(container_id, str)
        or not container_id
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or running is not True
        or not isinstance(restart_count, int)
        or isinstance(restart_count, bool)
        or restart_count < 0
        or not isinstance(started_at, str)
        or not started_at
        or not isinstance(image_id, str)
        or not image_id
    ):
        raise HarnessError("Selected runtime state contains malformed fields")

    return RuntimeState(container_id, pid, restart_count, started_at, image_id)


def parse_stop_state(payload: str) -> StopState:
    try:
        state = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HarnessError("Selected stop state is not valid JSON") from error
    if not isinstance(state, dict):
        raise HarnessError("Selected stop state must be a JSON object")
    running = state.get("running")
    restarting = state.get("restarting")
    exit_code = state.get("exit_code")
    oom_killed = state.get("oom_killed")
    finished_at = state.get("finished_at")
    if (
        running is not False
        or restarting is not False
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code < 0
        or oom_killed is not False
        or not isinstance(finished_at, str)
        or not finished_at
        or finished_at.startswith("0001-01-01")
    ):
        raise HarnessError(
            "Selected stop state contains malformed or non-graceful fields"
        )

    return StopState(running, exit_code, oom_killed, finished_at)


def parse_beat_snapshot(payload: str, expected_entries: frozenset[str]) -> BeatSnapshot:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HarnessError("Beat snapshot is not valid JSON") from error
    if not isinstance(decoded, dict) or not isinstance(decoded.get("entries"), dict):
        raise HarnessError("Beat snapshot must contain an entries object")
    entries = decoded["entries"]
    missing = expected_entries - set(entries)
    if missing:
        raise HarnessError(
            f"Beat snapshot missing expected entries: {sorted(missing)!r}"
        )
    cursors: list[tuple[str, str | None, int]] = []
    for name in sorted(entries):
        cursor = entries[name]
        if not isinstance(cursor, dict):
            raise HarnessError(f"Beat snapshot cursor for {name!r} must be an object")
        last_run_at = cursor.get("last_run_at")
        total_run_count = cursor.get("total_run_count")
        if last_run_at is not None and not isinstance(last_run_at, str):
            raise HarnessError(f"Beat snapshot last_run_at for {name!r} is malformed")
        if (
            not isinstance(total_run_count, int)
            or isinstance(total_run_count, bool)
            or total_run_count < 0
        ):
            raise HarnessError(
                f"Beat snapshot total_run_count for {name!r} is malformed"
            )
        cursors.append((name, last_run_at, total_run_count))

    return BeatSnapshot(entries=tuple(cursors))


def parse_pid_role(argv: list[str], service: str) -> str:  # noqa: C901
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise HarnessError(f"{service} PID 1 argv is empty or malformed")
    executable = Path(argv[0]).name
    if executable in {"sh", "bash", "dash", "ash"}:
        raise HarnessError(f"{service} PID 1 must not be a shell")
    semantic = argv[1:] if executable.startswith("python") else argv
    if not semantic:
        raise HarnessError(f"{service} PID 1 does not contain a command")
    command = Path(semantic[0]).name
    arguments = semantic[1:]
    if (
        service == "api"
        and command == "granian"
        and arguments[:3]
        == [
            "--interface",
            "wsgi",
            "settings.wsgi:application",
        ]
    ):
        return "granian"
    if (
        service == "relay"
        and command == "manage.py"
        and arguments[:1] == ["celery_outbox_relay"]
    ):
        return "outbox-relay"
    if command != "celery" or arguments[:2] != ["-A", "engram.celery_app"]:
        raise HarnessError(
            f"{service} PID 1 does not match its expected role: {argv!r}"
        )
    celery_args = arguments[2:]
    if service == "beat" and celery_args[:1] == ["beat"]:
        schedule = "/var/lib/engram-beat/celerybeat-schedule"
        schedule_values = _option_values(celery_args[1:], "--schedule")
        if schedule_values == [schedule]:
            return "celery-beat"
    if service.startswith("worker-") and celery_args[:1] == ["worker"]:
        queue = service.removeprefix("worker-")
        expected_queue = f"engram-{queue}"
        queue_values = _option_values(celery_args[1:], "-Q")
        if queue_values == [expected_queue]:
            return "celery-worker"
    raise HarnessError(f"{service} PID 1 does not match its expected role: {argv!r}")


def _option_values(arguments: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == option and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument.split("=", 1)[1])

    return values


class Harness:
    def __init__(
        self,
        *,
        project: str,
        env_file: Path,
        override_file: Path,
        generated_secrets: list[str],
    ) -> None:
        validate_project_name(project)
        _validate_absolute_canonical(env_file, "generated env file")
        _validate_absolute_canonical(override_file, "generated Compose override")
        self.project = project
        self.env_file = env_file
        self.override_file = override_file
        self.generated_secrets = generated_secrets
        self.deadline = Deadline(WORKLOAD_TIMEOUT, label="workload deadline")
        self.started_at = time.monotonic()
        self.phase_timings: dict[str, float] = {}
        self.server_url = ""

    @property
    def compose_prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-p",
            self.project,
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(self.override_file),
        ]

    @property
    def command_env(self) -> dict[str, str]:
        return deterministic_env(self.env_file)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path = ROOT,
        timeout: float = COMMAND_TIMEOUT,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        command_timeout = min(timeout, self.deadline.remaining())
        try:
            completed = subprocess.run(  # noqa: S603
                args,
                cwd=cwd,
                env=env or self.command_env,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired as error:
            command = redact_secrets(" ".join(args), self.generated_secrets)
            raise HarnessError(
                f"Command timed out after {command_timeout:.1f}s: {command}"
            ) from error
        result = CommandResult(
            args=args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise HarnessError(self._failure_message(result))

        return result

    def compose(
        self, *args: str, timeout: float = COMMAND_TIMEOUT, check: bool = True
    ) -> CommandResult:
        return self.run([*self.compose_prefix, *args], timeout=timeout, check=check)

    def compose_json(
        self, *args: str, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        result = self.compose(*args, timeout=timeout)
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as error:
            raise HarnessError(
                f"Expected JSON object from Compose command {args!r}"
            ) from error
        if not isinstance(payload, dict):
            raise HarnessError(f"Expected JSON object from Compose command {args!r}")

        return payload

    def refresh_api_origin(
        self,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> str:
        command_deadline = deadline or Deadline(
            timeout, label="API origin resolution deadline"
        )
        result = self.compose(
            "port",
            "api",
            "8000",
            timeout=command_deadline.remaining(),
        )
        lines = result.stdout.splitlines()
        match = (
            re.fullmatch(r"127\.0\.0\.1:([1-9]\d{0,4})", lines[0])
            if len(lines) == 1
            else None
        )
        if match is None or int(match.group(1)) > 65535:
            raise HarnessError("Unexpected loopback API port mapping")
        self.server_url = f"http://127.0.0.1:{match.group(1)}"

        return self.server_url

    def _failure_message(self, result: CommandResult) -> str:
        command = redact_secrets(" ".join(result.args), self.generated_secrets)
        stdout = redact_tail(result.stdout, self.generated_secrets)
        stderr = redact_tail(result.stderr, self.generated_secrets)

        return f"Command failed ({result.returncode}): {command}\nstdout tail:\n{stdout}\nstderr tail:\n{stderr}"

    def poll(
        self, label: str, timeout: float, probe: Callable[[float], object]
    ) -> object:
        expires_at = time.monotonic() + min(timeout, self.deadline.remaining())
        last_error = "probe did not run"
        while True:
            global_remaining = self.deadline.remaining()
            phase_remaining = expires_at - time.monotonic()
            if phase_remaining <= 0:
                break
            try:
                return probe(min(phase_remaining, global_remaining))
            except HarnessError as error:
                last_error = str(error)
                time.sleep(min(POLL_INTERVAL, max(0.0, expires_at - time.monotonic())))
        raise HarnessError(f"Timed out waiting for {label}: {last_error}")

    def phase(self, name: str, action: Callable[[], None]) -> None:
        progress(f"{name} started")
        started = time.monotonic()
        action()
        elapsed = time.monotonic() - started
        self.phase_timings[name] = elapsed
        progress(f"{name} passed in {elapsed:.1f}s")

    def api_shell_json(
        self, code: str, *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        return self.compose_json(
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

    def queue_state(self, *, timeout: float = COMMAND_TIMEOUT) -> QueueState:
        result = self.compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "-q",
            "-p",
            "engram",
            "list_queues",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "--formatter",
            "json",
            timeout=timeout,
        )

        return parse_queue_state(result.stdout, TARGET_QUEUE)

    def exact_state(
        self, project_id: str, run_id: str, *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        return self.api_shell_json(
            exact_state_query(project_id, run_id), timeout=timeout
        )

    def assert_exact_state(
        self,
        project_id: str,
        run_id: str,
        *,
        observations: int,
        outbox: int,
        versions: int,
        documents: int,
        candidates: int = 0,
        candidate_held_audits: int = 0,
        candidate_decision_work: int = 0,
        candidate_decision_outbox: int = 0,
        timeout: float = COMMAND_TIMEOUT,
    ) -> dict[str, object]:
        state = self.exact_state(project_id, run_id, timeout=timeout)
        expected = {
            "observations": observations,
            "outbox": outbox,
            "candidates": candidates,
            "candidate_held_audits": candidate_held_audits,
            "candidate_decision_work": candidate_decision_work,
            "candidate_decision_outbox": candidate_decision_outbox,
            "memories": versions,
            "versions": versions,
            "documents": documents,
            "linked_documents": documents,
        }
        for key, value in expected.items():
            if state.get(key) != value:
                raise HarnessError(
                    f"Exact event {run_id} expected {key}={value}, got {state!r}"
                )

        return state

    def assert_queue(
        self, *, ready: int, unacknowledged: int, timeout: float = COMMAND_TIMEOUT
    ) -> QueueState:
        state = self.queue_state(timeout=timeout)
        expected = QueueState(ready, unacknowledged)
        if state != expected:
            raise HarnessError(f"Expected queue {expected!r}, got {state!r}")

        return state

    def container_id(self, service: str, *, timeout: float = COMMAND_TIMEOUT) -> str:
        result = self.compose("ps", "-a", "-q", service, timeout=timeout)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(ids) != 1:
            raise HarnessError(f"Expected one {service} container, got {ids!r}")

        return ids[0]

    def inspect_selected(
        self, container_id: str, template: str, *, timeout: float = COMMAND_TIMEOUT
    ) -> str:
        return self.run(
            ["docker", "inspect", "--format", template, container_id], timeout=timeout
        ).stdout

    def runtime_state(
        self,
        service: str,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> RuntimeState:
        command_deadline = deadline or Deadline(
            timeout, label=f"{service} runtime state deadline"
        )
        container_id = self.container_id(service, timeout=command_deadline.remaining())
        template = (
            '{"container_id":{{json .Id}},"pid":{{json .State.Pid}},'
            '"running":{{json .State.Running}},"restart_count":{{json .RestartCount}},'
            '"started_at":{{json .State.StartedAt}},"image_id":{{json .Image}}}'
        )

        return parse_runtime_state(
            self.inspect_selected(
                container_id,
                template,
                timeout=command_deadline.remaining(),
            )
        )

    def stop_service(
        self,
        service: str,
        timeout: float,
        *,
        deadline: Deadline | None = None,
    ) -> tuple[StopState, float]:
        global_remaining = self.deadline.remaining()
        phase_remaining = deadline.remaining() if deadline else timeout
        allowed = min(timeout, global_remaining, phase_remaining)
        if allowed < 1:
            raise HarnessError(f"Insufficient global deadline to stop {service}")
        container_id = self.container_id(
            service,
            timeout=min(
                10.0,
                allowed,
                deadline.remaining() if deadline else allowed,
            ),
        )
        template = (
            '{"running":{{json .State.Running}},"restarting":{{json .State.Restarting}},'
            '"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}},'
            '"finished_at":{{json .State.FinishedAt}}}'
        )
        before = json.loads(
            self.inspect_selected(
                container_id,
                template,
                timeout=min(
                    10.0,
                    allowed,
                    deadline.remaining() if deadline else allowed,
                ),
            )
        )
        before_finished = (
            before.get("finished_at") if isinstance(before, dict) else None
        )
        required_budget = timeout + STOP_CLI_OVERHEAD + STOP_POST_INSPECT_TIMEOUT
        if self.deadline.remaining() < required_budget:
            raise HarnessError(f"Insufficient global deadline to stop {service}")
        if deadline and deadline.remaining() < required_budget:
            raise HarnessError(f"Insufficient phase deadline to stop {service}")
        command = [
            "docker",
            "stop",
            "--signal",
            "SIGTERM",
            "--timeout",
            str(int(timeout)),
            container_id,
        ]
        started = time.monotonic()
        self.run(
            command,
            timeout=timeout + STOP_CLI_OVERHEAD,
        )
        observed = time.monotonic() - started
        state = parse_stop_state(
            self.inspect_selected(
                container_id,
                template,
                timeout=min(
                    STOP_POST_INSPECT_TIMEOUT,
                    self.deadline.remaining(),
                    deadline.remaining() if deadline else STOP_POST_INSPECT_TIMEOUT,
                ),
            )
        )
        if state.exit_code != 0:
            raise HarnessError(
                f"{service} exited non-gracefully with code {state.exit_code}"
            )
        if state.finished_at == before_finished:
            raise HarnessError(f"{service} FinishedAt did not change")

        return state, observed

    def wait_container_healthy(self, service: str, timeout: float) -> None:
        def probe(remaining: float) -> object:
            probe_deadline = Deadline(
                remaining, label=f"{service} health probe deadline"
            )
            container_id = self.container_id(
                service,
                timeout=probe_deadline.remaining(),
            )
            template = '{"status":{{json .State.Status}},"health":{{json .State.Health.Status}}}'
            try:
                payload = json.loads(
                    self.inspect_selected(
                        container_id,
                        template,
                        timeout=probe_deadline.remaining(),
                    )
                )
            except json.JSONDecodeError as error:
                raise HarnessError(f"{service} inspect result is malformed") from error
            if not isinstance(payload, dict):
                raise HarnessError(f"{service} inspect result is malformed")
            if payload.get("status") != "running":
                raise HarnessError(f"{service} is not running")
            health = payload.get("health")
            if health and health != "healthy":
                raise HarnessError(f"{service} is not healthy")

            return payload

        self.poll(f"{service} health", timeout, probe)

    def rabbit_state(
        self,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> RabbitState:
        command_deadline = deadline or Deadline(timeout, label="Rabbit state deadline")
        container_id = self.container_id(
            "rabbitmq",
            timeout=command_deadline.remaining(),
        )
        volume_name = self.volume_name(
            "rabbitmq",
            "/var/lib/rabbitmq",
            deadline=command_deadline,
        )
        nodename = (
            self.compose(
                "exec",
                "-T",
                "rabbitmq",
                "rabbitmqctl",
                "-q",
                "eval",
                "node().",
                timeout=command_deadline.remaining(),
            )
            .stdout.strip()
            .strip("'")
        )
        if not nodename:
            raise HarnessError("Rabbit nodename is empty")

        return RabbitState(container_id, nodename, volume_name)

    def volume_name(
        self,
        service: str,
        destination: str,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> str:
        command_deadline = deadline or Deadline(
            timeout, label=f"{service} volume deadline"
        )
        container_id = self.container_id(service, timeout=command_deadline.remaining())
        template = "{{range .Mounts}}{{json .Type}}\t{{json .Name}}\t{{json .Destination}}{{println}}{{end}}"
        output = self.inspect_selected(
            container_id,
            template,
            timeout=command_deadline.remaining(),
        )
        names: list[str] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                mount_type, name, mount_destination = json.loads(
                    f"[{line.replace(chr(9), ',')}]"
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise HarnessError(
                    f"{service} selected mount identity is malformed"
                ) from error
            if (
                mount_type == "volume"
                and mount_destination == destination
                and isinstance(name, str)
            ):
                names.append(name)
        if len(names) != 1:
            raise HarnessError(
                f"{service} volume at {destination} is ambiguous: {names!r}"
            )

        return names[0]

    def pid_argv(
        self,
        service: str,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> list[str]:
        command_deadline = deadline or Deadline(
            timeout, label=f"{service} PID argv deadline"
        )
        code = (
            "import json; "
            "print(json.dumps([p.decode() for p in open('/proc/1/cmdline', 'rb').read().split(b'\\0') if p]))"
        )
        result = self.run(
            [
                "docker",
                "exec",
                self.container_id(service, timeout=command_deadline.remaining()),
                "python",
                "-c",
                code,
            ],
            timeout=command_deadline.remaining(),
        )
        try:
            argv = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise HarnessError(f"{service} PID 1 argv is not JSON") from error
        if not isinstance(argv, list):
            raise HarnessError(f"{service} PID 1 argv must be a list")

        return argv

    def assert_role(
        self,
        service: str,
        *,
        timeout: float = COMMAND_TIMEOUT,
        deadline: Deadline | None = None,
    ) -> str:
        return parse_pid_role(
            self.pid_argv(service, timeout=timeout, deadline=deadline),
            service,
        )

    def wait_role(
        self,
        service: str,
        timeout: float = 90.0,
        *,
        deadline: Deadline | None = None,
    ) -> str:
        command_deadline = deadline or Deadline(
            timeout, label=f"{service} role deadline"
        )
        return str(
            self.poll(
                f"{service} PID 1 role",
                command_deadline.remaining(),
                lambda remaining: self.assert_role(
                    service,
                    timeout=remaining,
                    deadline=command_deadline,
                ),
            )
        )

    def submit_hook(
        self, config_dir: Path, run_id: str, *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, object]:
        env = deterministic_env(
            self.env_file, extra={"PYTHONPATH": str(ROOT / "packages/cli")}
        )
        result = self.run(
            [
                sys.executable,
                "-m",
                "engram_cli",
                "hook",
                "post-tool-use",
                "--config-dir",
                str(config_dir),
            ],
            env=env,
            input_text=json.dumps(post_tool_use_payload(run_id)),
            timeout=timeout,
            check=False,
        )

        return parse_hook_result(result, self.generated_secrets)

    def dump_failure_logs(self) -> None:
        for service in FAILURE_LOG_SERVICES:
            try:
                completed = subprocess.run(  # noqa: S603
                    [*self.compose_prefix, "logs", "--no-color", "--tail=80", service],
                    cwd=ROOT,
                    env=self.command_env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=FAILURE_LOG_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                progress(
                    f"{service} log tail timed out after {FAILURE_LOG_TIMEOUT:.0f}s"
                )
                continue
            result = CommandResult(
                (), completed.returncode, completed.stdout, completed.stderr
            )
            output = redact_tail(
                result.stdout + result.stderr,
                self.generated_secrets,
            )
            if output:
                progress(f"{service} log tail:\n{output}")


def parse_last_json_object(output: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} did not produce a JSON object") from error
    if not isinstance(payload, dict):
        raise HarnessError(f"{label} did not produce a JSON object")

    return payload


def parse_hook_result(
    result: CommandResult, generated_secrets: Sequence[str]
) -> dict[str, object]:
    try:
        return parse_last_json_object(result.stdout, "hook response")
    except HarnessError as error:
        stderr = redact_tail(
            result.stderr,
            generated_secrets,
        )
        raise HarnessError(
            "hook response did not produce a JSON object; "
            f"returncode={result.returncode}; stderr tail:\n{stderr}"
        ) from error


def post_tool_use_payload(run_id: str) -> dict[str, object]:
    return {
        "session_id": f"d1-session-{run_id}",
        "event_id": f"d1-event-{run_id}",
        "idempotency_key": f"d1-idempotency-{run_id}",
        "request_id": f"d1-request-{run_id}",
        "payload": {
            "tool_name": "bash",
            "tool_input": {"command": "pytest durability"},
            "tool_response": {"exit_code": 0},
        },
        "observation": {
            "type": "tool_use",
            "title": f"D1 durability observation [{run_id}]",
            "body": f"Disposable D1 durability evidence for unique run {run_id}. " * 2,
            "files_read": ["scripts/e2e_runtime_durability.py"],
            "files_modified": [],
        },
        "repository_root": "/workspace/engram",
        "branch": "d1-disposable",
        "cwd": "/workspace/engram",
    }


def exact_state_query(project_id: str, run_id: str) -> str:
    client_event_id = f"d1-event-{run_id}"
    request_id = f"d1-request-{run_id}"

    return f"""
import json
from django.db.models import F
from django_celery_outbox.models import CeleryOutbox
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Observation,
    RetrievalDocument,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)

project_id = {json.dumps(project_id)}
client_event_id = {json.dumps(client_event_id)}
request_id = {json.dumps(request_id)}
observations = Observation.objects.filter(
    project_id=project_id,
    raw_event__client_event_id=client_event_id,
    raw_event__request_id=request_id,
)
observation_ids = [str(value) for value in observations.values_list('id', flat=True)]
work_ids = [
    str(value)
    for value in WorkflowWork.objects.filter(
        project_id=project_id,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        subject_type=WorkflowSubjectType.OBSERVATION,
        subject_id__in=observation_ids,
    ).values_list('id', flat=True)
]
outbox = sum(
    1
    for row in CeleryOutbox.objects.filter(task_name={json.dumps(TARGET_TASK)}).only('args')
    if row.args in ([work_id] for work_id in work_ids)
)
candidates = MemoryCandidate.objects.filter(
    project_id=project_id,
    source_observation__raw_event__client_event_id=client_event_id,
    source_observation__raw_event__request_id=request_id,
    status='proposed',
    decision_work_contract_version=0,
    promoted_memory__isnull=True,
)
candidate_ids = [str(value) for value in candidates.values_list('id', flat=True)]
candidate_held_audits = AuditEvent.objects.filter(
    project_id=project_id,
    event_type='MemoryCandidateHeldForReview',
    target_type='memory_candidate',
    target_id__in=candidate_ids,
    result='recorded',
)
candidate_decision_work = WorkflowWork.objects.filter(
    project_id=project_id,
    work_type=WorkflowWorkType.CANDIDATE_DECISION,
    subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
    subject_id__in=candidate_ids,
)
candidate_decision_work_ids = [
    str(value)
    for value in candidate_decision_work.values_list('id', flat=True)
]
candidate_decision_outbox = sum(
    1
    for row in CeleryOutbox.objects.filter(
        task_name={json.dumps(TARGET_CANDIDATE_DECISION_TASK)}
    ).only('args')
    if row.args in ([work_id] for work_id in candidate_decision_work_ids)
)
versions = MemoryVersion.objects.select_related('memory', 'source_observation__raw_event').filter(
    project_id=project_id,
    memory__project_id=project_id,
    memory__status=MemoryStatus.APPROVED,
    memory__stale=False,
    memory__refuted=False,
    version=F('memory__current_version'),
    source_observation__raw_event__client_event_id=client_event_id,
    source_observation__raw_event__request_id=request_id,
)
memories = Memory.objects.filter(
    project_id=project_id,
    status=MemoryStatus.APPROVED,
    stale=False,
    refuted=False,
    id__in=versions.values('memory_id'),
)
documents = RetrievalDocument.objects.filter(
    project_id=project_id,
    memory_id__in=memories.values('id'),
    memory_version_id__in=versions.values('id'),
    stale=False,
    refuted=False,
)
linked_documents = sum(
    1
    for document in documents.only('source_observation_ids')
    if any(observation_id in document.source_observation_ids for observation_id in observation_ids)
)
print(json.dumps({{
    'observations': observations.count(),
    'outbox': outbox,
    'candidates': candidates.count(),
    'candidate_held_audits': candidate_held_audits.count(),
    'candidate_decision_work': candidate_decision_work.count(),
    'candidate_decision_outbox': candidate_decision_outbox,
    'memories': memories.count(),
    'versions': versions.count(),
    'documents': documents.count(),
    'linked_documents': linked_documents,
    'observation_id': observation_ids[0] if len(observation_ids) == 1 else None,
    'memory_id': str(memories.values_list('id', flat=True).first()) if memories.count() == 1 else None,
    'version_id': str(versions.values_list('id', flat=True).first()) if versions.count() == 1 else None,
    'document_id': str(documents.values_list('id', flat=True).first()) if linked_documents == 1 else None,
}}))
"""


def selected_celery_flags_code() -> str:
    return (
        "import json; from engram.celery_app import app; "
        "print(json.dumps({'broker_connection_retry_on_startup': app.conf.broker_connection_retry_on_startup, "
        "'broker_connection_retry': app.conf.broker_connection_retry, "
        "'broker_connection_max_retries': app.conf.broker_connection_max_retries, "
        "'worker_enable_soft_shutdown_on_idle': app.conf.worker_enable_soft_shutdown_on_idle}))"
    )


BEAT_SNAPSHOT_CODE = """
import json
import shelve

with shelve.open('/beat/celerybeat-schedule', flag='r') as store:
    entries = store.get('entries')
    if not isinstance(entries, dict):
        raise SystemExit('schedule entries missing')
    payload = {}
    for name, entry in entries.items():
        last_run_at = getattr(entry, 'last_run_at', None)
        payload[name] = {
            'last_run_at': last_run_at.isoformat() if last_run_at is not None else None,
            'total_run_count': getattr(entry, 'total_run_count', None),
        }
print(json.dumps({'entries': payload}, sort_keys=True))
"""


def read_beat_snapshot(
    harness: Harness,
    volume_name: str,
    image_id: str,
    *,
    deadline: Deadline,
) -> BeatSnapshot:
    helper_name = f"{harness.project}-beat-snapshot-{secrets.token_hex(4)}"
    harness.run(
        [
            "docker",
            "create",
            "--name",
            helper_name,
            "--label",
            f"com.docker.compose.project={harness.project}",
            "--label",
            "engram.d1.disposable=true",
            "--restart",
            "no",
            "--mount",
            f"type=volume,src={volume_name},dst=/beat,readonly",
            image_id,
            "python",
            "-c",
            BEAT_SNAPSHOT_CODE,
        ],
        timeout=deadline.remaining(),
    )
    try:
        result = harness.run(
            ["docker", "start", "--attach", helper_name],
            timeout=deadline.remaining(),
        )
    finally:
        try:
            cleanup_timeout = min(5.0, deadline.remaining())
        except HarnessError:
            cleanup_timeout = 1.0
        subprocess.run(  # noqa: S603
            ["docker", "rm", "-f", helper_name],
            cwd=ROOT,
            env=harness.command_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=cleanup_timeout,
        )

    return parse_beat_snapshot(
        result.stdout.strip().splitlines()[-1], EXPECTED_BEAT_ENTRIES
    )


def beat_snapshot_evidence(snapshot: BeatSnapshot) -> dict[str, object]:
    encoded = json.dumps(snapshot.entries, separators=(",", ":"))

    return {
        "entries": len(snapshot.entries),
        "digest": hashlib.sha256(encoded.encode()).hexdigest()[:16],
    }


def connect_cli(
    harness: Harness,
    config_dir: Path,
    api_key: str,
    bootstrap: dict[str, object],
    *,
    timeout: float = COMMAND_TIMEOUT,
    deadline: Deadline | None = None,
) -> None:
    command_deadline = deadline or Deadline(timeout, label="CLI connect deadline")
    project_id = required_string(bootstrap, "project_id")
    team_id = required_string(bootstrap, "team_id")
    env = deterministic_env(
        harness.env_file, extra={"PYTHONPATH": str(ROOT / "packages/cli")}
    )
    harness.run(
        [
            sys.executable,
            "-m",
            "engram_cli",
            "connect",
            "--server",
            harness.server_url,
            "--api-key",
            api_key,
            "--project",
            project_id,
            "--team",
            team_id,
            "--agent",
            "codex",
            "--agent-version",
            "d1-fault-harness",
            "--config-dir",
            str(config_dir),
        ],
        env=env,
        timeout=command_deadline.remaining(),
    )


def required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HarnessError(f"Missing required string {key!r}")

    return value


def wait_exact(
    harness: Harness,
    project_id: str,
    run_id: str,
    *,
    observations: int,
    outbox: int,
    versions: int,
    documents: int,
    candidates: int = 0,
    candidate_held_audits: int = 0,
    candidate_decision_work: int = 0,
    candidate_decision_outbox: int = 0,
    timeout: float = COMMAND_TIMEOUT,
) -> dict[str, object]:
    return dict(
        harness.poll(
            f"exact state for {run_id}",
            timeout,
            lambda remaining: harness.assert_exact_state(
                project_id,
                run_id,
                observations=observations,
                outbox=outbox,
                versions=versions,
                documents=documents,
                candidates=candidates,
                candidate_held_audits=candidate_held_audits,
                candidate_decision_work=candidate_decision_work,
                candidate_decision_outbox=candidate_decision_outbox,
                timeout=remaining,
            ),
        )
    )


def wait_queue(
    harness: Harness, *, ready: int, unacknowledged: int, timeout: float
) -> QueueState:
    return harness.poll(
        f"queue ready={ready} unacknowledged={unacknowledged}",
        timeout,
        lambda remaining: harness.assert_queue(
            ready=ready,
            unacknowledged=unacknowledged,
            timeout=remaining,
        ),
    )  # type: ignore[return-value]


def assert_fault_baseline(harness: Harness, project_id: str, run_id: str) -> None:
    deadline = Deadline(60, label=f"{run_id} baseline deadline")
    wait_queue(
        harness,
        ready=0,
        unacknowledged=0,
        timeout=deadline.remaining(),
    )
    harness.assert_exact_state(
        project_id,
        run_id,
        observations=0,
        outbox=0,
        versions=0,
        documents=0,
        timeout=deadline.remaining(),
    )


def fault_a(harness: Harness, config_dir: Path, project_id: str, run_id: str) -> None:
    assert_fault_baseline(harness, project_id, run_id)
    harness.compose("stop", "worker-near-realtime", "relay", timeout=100)
    package_deadline = Deadline(30, label="Fault A package deadline")
    response = harness.submit_hook(
        config_dir,
        run_id,
        timeout=package_deadline.remaining(),
    )
    if response.get("status") != "accepted":
        raise HarnessError(f"Fault A hook was not accepted: {response!r}")
    wait_exact(
        harness,
        project_id,
        run_id,
        observations=1,
        outbox=1,
        versions=0,
        documents=0,
        timeout=package_deadline.remaining(),
    )
    harness.assert_queue(
        ready=0,
        unacknowledged=0,
        timeout=package_deadline.remaining(),
    )

    relay_deadline = Deadline(90, label="Fault A relay deadline")
    harness.compose("start", "relay", timeout=relay_deadline.remaining())
    wait_exact(
        harness,
        project_id,
        run_id,
        observations=1,
        outbox=0,
        versions=0,
        documents=0,
        timeout=relay_deadline.remaining(),
    )
    wait_queue(harness, ready=1, unacknowledged=0, timeout=relay_deadline.remaining())
    rabbit_deadline = Deadline(120, label="Fault A Rabbit deadline")
    before = harness.rabbit_state(deadline=rabbit_deadline)
    harness.compose(
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "rabbitmq",
        timeout=rabbit_deadline.remaining(),
    )
    harness.wait_container_healthy("rabbitmq", rabbit_deadline.remaining())
    after = harness.rabbit_state(deadline=rabbit_deadline)
    if before.container_id == after.container_id:
        raise HarnessError("Fault A Rabbit container did not change")
    if before.nodename != after.nodename or before.volume_name != after.volume_name:
        raise HarnessError(
            f"Fault A Rabbit identity changed: before={before!r}, after={after!r}"
        )
    wait_queue(harness, ready=1, unacknowledged=0, timeout=rabbit_deadline.remaining())
    result_deadline = Deadline(180, label="Fault A result deadline")
    harness.compose(
        "start", "worker-near-realtime", timeout=result_deadline.remaining()
    )
    terminal = wait_exact(
        harness,
        project_id,
        run_id,
        observations=1,
        outbox=0,
        versions=0,
        documents=0,
        candidates=1,
        candidate_held_audits=1,
        candidate_decision_outbox=0,
        timeout=result_deadline.remaining(),
    )
    wait_queue(harness, ready=0, unacknowledged=0, timeout=result_deadline.remaining())
    progress(
        "Fault A evidence="
        + json.dumps(
            {
                "rabbit_before": {
                    "container": before.container_id[:12],
                    "nodename": before.nodename,
                    "volume": before.volume_name,
                },
                "rabbit_after": {
                    "container": after.container_id[:12],
                    "nodename": after.nodename,
                    "volume": after.volume_name,
                },
                "terminal": terminal,
            },
            sort_keys=True,
        )
    )


def fault_b(harness: Harness, config_dir: Path, project_id: str, run_id: str) -> None:
    assert_fault_baseline(harness, project_id, run_id)
    before = {
        service: harness.runtime_state(service)
        for service in ("worker-near-realtime", "relay")
    }
    harness.compose("stop", "rabbitmq", timeout=60)
    outbox_deadline = Deadline(30, label="Fault B outbox creation deadline")
    response = harness.submit_hook(
        config_dir, run_id, timeout=outbox_deadline.remaining()
    )
    if response.get("status") != "accepted":
        raise HarnessError(f"Fault B hook was not accepted: {response!r}")
    wait_exact(
        harness,
        project_id,
        run_id,
        observations=1,
        outbox=1,
        versions=0,
        documents=0,
        timeout=outbox_deadline.remaining(),
    )
    recovery_deadline = Deadline(180, label="Fault B recovery deadline")
    harness.compose("start", "rabbitmq", timeout=recovery_deadline.remaining())
    harness.wait_container_healthy("rabbitmq", recovery_deadline.remaining())
    flags_result = harness.run(
        [
            "docker",
            "exec",
            before["worker-near-realtime"].container_id,
            "python",
            "-c",
            selected_celery_flags_code(),
        ],
        timeout=recovery_deadline.remaining(),
    )
    flags = parse_last_json_object(
        flags_result.stdout, "selected Celery reconnect flags"
    )
    expected_flags = {
        "broker_connection_retry_on_startup": True,
        "broker_connection_retry": True,
        "broker_connection_max_retries": None,
        "worker_enable_soft_shutdown_on_idle": True,
    }
    if flags != expected_flags:
        raise HarnessError(f"Fault B reconnect flags differ: {flags!r}")
    terminal = wait_exact(
        harness,
        project_id,
        run_id,
        observations=1,
        outbox=0,
        versions=0,
        documents=0,
        candidates=1,
        candidate_held_audits=1,
        candidate_decision_outbox=0,
        timeout=recovery_deadline.remaining(),
    )
    wait_queue(
        harness, ready=0, unacknowledged=0, timeout=recovery_deadline.remaining()
    )
    after = {
        service: harness.runtime_state(service, deadline=recovery_deadline)
        for service in ("worker-near-realtime", "relay")
    }
    changed = [service for service in before if before[service] != after[service]]
    if changed:
        raise HarnessError(
            f"Fault B app processes changed across broker outage: {changed!r}"
        )
    progress(
        "Fault B runtime_identity="
        + json.dumps(
            {
                "processes": {
                    service: {
                        "before": {
                            "container": before[service].container_id[:12],
                            "pid": before[service].pid,
                            "restarts": before[service].restart_count,
                            "started_at": before[service].started_at,
                            "image": before[service].image_id[:19],
                        },
                        "after": {
                            "container": state.container_id[:12],
                            "pid": state.pid,
                            "restarts": state.restart_count,
                            "started_at": state.started_at,
                            "image": state.image_id[:19],
                        },
                    }
                    for service, state in after.items()
                },
                "reconnect_flags": flags,
            },
            sort_keys=True,
        )
    )
    progress("Fault B terminal=" + json.dumps(terminal, sort_keys=True))


def fault_c(harness: Harness) -> None:
    def readiness_probe(remaining: float) -> object:
        harness.compose(
            "exec",
            "-T",
            "beat",
            "python",
            "-c",
            "from pathlib import Path; assert Path('/tmp/engram_celery_ready').is_file(); "
            "assert Path('/var/lib/engram-beat/celerybeat-schedule').is_file()",
            timeout=remaining,
        )

        return True

    def singleton(deadline: Deadline) -> tuple[int, int]:
        ids = [
            line
            for line in harness.compose(
                "ps",
                "-q",
                "--status",
                "running",
                "beat",
                timeout=deadline.remaining(),
            ).stdout.splitlines()
            if line
        ]
        if len(ids) != 1:
            raise HarnessError(f"Fault C expected one running Beat, got {ids!r}")
        top = harness.run(
            ["docker", "top", ids[0], "-eo", "pid,args"],
            timeout=deadline.remaining(),
        ).stdout.splitlines()
        beat_processes = 0
        for row in top[1:]:
            fields = row.strip().split(maxsplit=1)
            if len(fields) != 2:
                continue
            try:
                parse_pid_role(shlex.split(fields[1]), "beat")
            except (HarnessError, ValueError):
                continue
            beat_processes += 1
        if beat_processes != 1:
            raise HarnessError(
                f"Fault C expected one Beat process, got {beat_processes}"
            )

        return len(ids), beat_processes

    schedule_deadline = Deadline(60, label="Fault C schedule deadline")
    harness.poll(
        "Beat readiness file and schedule",
        schedule_deadline.remaining(),
        readiness_probe,
    )
    recreate_deadline = Deadline(90, label="Fault C capture and recreate deadline")
    beat_image_id = harness.runtime_state("beat", deadline=recreate_deadline).image_id
    stop_state, stop_elapsed = harness.stop_service(
        "beat",
        25,
        deadline=recreate_deadline,
    )
    volume_before = harness.volume_name(
        "beat",
        "/var/lib/engram-beat",
        deadline=recreate_deadline,
    )
    snapshot_before = read_beat_snapshot(
        harness,
        volume_before,
        beat_image_id,
        deadline=recreate_deadline,
    )
    harness.compose("rm", "-f", "beat", timeout=recreate_deadline.remaining())
    harness.compose(*BEAT_RECREATE_ARGS, timeout=recreate_deadline.remaining())
    volume_after = harness.volume_name(
        "beat",
        "/var/lib/engram-beat",
        deadline=recreate_deadline,
    )
    if volume_after != volume_before:
        raise HarnessError(
            f"Fault C Beat volume changed: {volume_before!r} -> {volume_after!r}"
        )
    snapshot_after = read_beat_snapshot(
        harness,
        volume_after,
        beat_image_id,
        deadline=recreate_deadline,
    )
    if snapshot_after != snapshot_before:
        raise HarnessError(
            "Fault C Beat cursor snapshot changed while Beat was stopped"
        )
    stopped = json.loads(
        harness.inspect_selected(
            harness.container_id("beat", timeout=recreate_deadline.remaining()),
            '{"running":{{json .State.Running}}}',
            timeout=recreate_deadline.remaining(),
        )
    )
    if not isinstance(stopped, dict) or stopped.get("running") is not False:
        raise HarnessError("Fault C replacement Beat started before cursor comparison")
    restart_deadline = Deadline(90, label="Fault C post-restart deadline")
    harness.compose("start", "beat", timeout=restart_deadline.remaining())
    harness.poll(
        "recreated Beat readiness", restart_deadline.remaining(), readiness_probe
    )
    harness.wait_role("beat", deadline=restart_deadline)
    singleton(restart_deadline)
    post_stop_state, post_stop_elapsed = harness.stop_service(
        "beat",
        25,
        deadline=restart_deadline,
    )
    snapshot_post_restart = read_beat_snapshot(
        harness,
        volume_after,
        beat_image_id,
        deadline=restart_deadline,
    )
    harness.compose("start", "beat", timeout=restart_deadline.remaining())
    harness.poll("final Beat readiness", restart_deadline.remaining(), readiness_probe)
    harness.wait_role("beat", deadline=restart_deadline)
    container_count, beat_processes = singleton(restart_deadline)
    progress(
        "Fault C evidence="
        + json.dumps(
            {
                "volume": volume_after,
                "snapshot_before": beat_snapshot_evidence(snapshot_before),
                "snapshot_after": beat_snapshot_evidence(snapshot_after),
                "snapshot_post_restart": beat_snapshot_evidence(snapshot_post_restart),
                "containers": container_count,
                "processes": beat_processes,
                "stop_exit": stop_state.exit_code,
                "stop_elapsed": round(stop_elapsed, 3),
                "post_restart_stop_exit": post_stop_state.exit_code,
                "post_restart_stop_elapsed": round(post_stop_elapsed, 3),
            },
            sort_keys=True,
        )
    )


def _assert_service_registration(
    harness: Harness,
    service: str,
    *,
    timeout: float = 90,
) -> None:
    if service == "api":
        harness.wait_container_healthy("api", timeout)
    elif service == "beat":
        harness.poll(
            "Beat registration",
            timeout,
            lambda remaining: harness.compose(
                "exec",
                "-T",
                "beat",
                "python",
                "-c",
                "from pathlib import Path; assert Path('/tmp/engram_celery_ready').is_file()",
                timeout=remaining,
            ),
        )
    elif service == "worker-near-realtime":

        def worker_probe(remaining: float) -> object:
            result = harness.compose(
                "exec",
                "-T",
                service,
                "celery",
                "-A",
                "engram.celery_app",
                "inspect",
                "active_queues",
                "--timeout=5",
                timeout=remaining,
            )
            if TARGET_QUEUE not in result.stdout:
                raise HarnessError(
                    "Near-realtime worker did not register its target queue"
                )

            return result

        harness.poll("near-realtime worker registration", timeout, worker_probe)


def fault_d(
    harness: Harness,
    config_dir: Path,
    project_id: str,
    relay_run_id: str,
    *,
    api_key: str,
    bootstrap: dict[str, object],
) -> None:
    services = (
        ("api", 40),
        ("relay", 50),
        ("beat", 25),
        ("worker-near-realtime", 90),
    )
    for service, stop_timeout in services:
        harness.wait_role(service)
        if service == "relay":
            assert_fault_baseline(harness, project_id, relay_run_id)
            harness.compose("stop", "worker-near-realtime", timeout=90)
        state, elapsed = harness.stop_service(service, stop_timeout)
        if service == "relay":
            relay_deadline = Deadline(180, label="Fault D relay registration deadline")
            response = harness.submit_hook(
                config_dir,
                relay_run_id,
                timeout=relay_deadline.remaining(),
            )
            if response.get("status") != "accepted":
                raise HarnessError(f"Fault D relay hook was not accepted: {response!r}")
            wait_exact(
                harness,
                project_id,
                relay_run_id,
                observations=1,
                outbox=1,
                versions=0,
                documents=0,
                timeout=relay_deadline.remaining(),
            )
            harness.assert_queue(
                ready=0,
                unacknowledged=0,
                timeout=relay_deadline.remaining(),
            )
        progress(
            f"Fault D {service} stop="
            + json.dumps(
                {
                    "elapsed": round(elapsed, 3),
                    "exit": state.exit_code,
                    "oom": state.oom_killed,
                },
                sort_keys=True,
            )
        )
        restart_deadline = (
            relay_deadline
            if service == "relay"
            else Deadline(90, label=f"Fault D {service} restart deadline")
        )
        harness.compose("start", service, timeout=restart_deadline.remaining())
        harness.wait_role(service, deadline=restart_deadline)
        if service == "api":
            _assert_service_registration(
                harness,
                service,
                timeout=restart_deadline.remaining(),
            )
            harness.refresh_api_origin(deadline=restart_deadline)
            connect_cli(
                harness,
                config_dir,
                api_key,
                bootstrap,
                deadline=restart_deadline,
            )
            progress("Fault D api functional_registration=cli_connect_dry_run")
        elif service == "relay":
            wait_exact(
                harness,
                project_id,
                relay_run_id,
                observations=1,
                outbox=0,
                versions=0,
                documents=0,
                timeout=relay_deadline.remaining(),
            )
            wait_queue(
                harness,
                ready=1,
                unacknowledged=0,
                timeout=relay_deadline.remaining(),
            )
            progress("Fault D relay functional_registration=outbox_drained_queue_ready")
            harness.compose(
                "start",
                "worker-near-realtime",
                timeout=relay_deadline.remaining(),
            )
            _assert_service_registration(
                harness,
                "worker-near-realtime",
                timeout=relay_deadline.remaining(),
            )
            wait_exact(
                harness,
                project_id,
                relay_run_id,
                observations=1,
                outbox=0,
                versions=0,
                documents=0,
                candidates=1,
                candidate_held_audits=1,
                candidate_decision_outbox=0,
                timeout=relay_deadline.remaining(),
            )
            wait_queue(
                harness,
                ready=0,
                unacknowledged=0,
                timeout=relay_deadline.remaining(),
            )
        else:
            _assert_service_registration(
                harness,
                service,
                timeout=restart_deadline.remaining(),
            )


def progress(message: str) -> None:
    print(f"[engram-d1] {message}", flush=True)  # noqa: T201


def cleanup(
    project: str, env_file: Path, generated_secrets: Sequence[str]
) -> tuple[bool, str]:
    backend_image = project_backend_image(project)
    command = cleanup_command(
        project=project, compose_file=COMPOSE_FILE, env_file=env_file
    )
    env = deterministic_env(env_file)
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=CLEANUP_COMPOSE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        output = (
            f"guarded Compose cleanup timed out after {CLEANUP_COMPOSE_TIMEOUT:.0f}s"
        )
    else:
        output = redact_tail(
            completed.stdout + completed.stderr,
            generated_secrets,
        )
        output += f"\ncompose_down_exit={completed.returncode}"
    filters = {
        "containers": [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        "networks": [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        "volumes": [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        "images": [
            "docker",
            "image",
            "ls",
            "--quiet",
            "--no-trunc",
            backend_image,
        ],
    }
    label_formats = {
        "containers": [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            '{{index .Config.Labels "com.docker.compose.project"}}',
        ],
        "networks": [
            "docker",
            "network",
            "inspect",
            "--format",
            '{{index .Labels "com.docker.compose.project"}}',
        ],
        "volumes": [
            "docker",
            "volume",
            "inspect",
            "--format",
            '{{index .Labels "com.docker.compose.project"}}',
        ],
    }
    removals = {
        "containers": ["docker", "rm", "-f"],
        "networks": ["docker", "network", "rm"],
        "volumes": ["docker", "volume", "rm"],
    }
    expires_at = time.monotonic() + CLEANUP_FALLBACK_TIMEOUT

    def cleanup_timeout() -> float:
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError("Cleanup fallback deadline expired")

        return min(5.0, remaining)

    residual: dict[str, list[str]] = {}
    while time.monotonic() < expires_at:
        residual = {}
        for label, query in filters.items():
            try:
                timeout = cleanup_timeout()
                query_result = subprocess.run(  # noqa: S603
                    query,
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout,
                )
            except (HarnessError, subprocess.TimeoutExpired):
                residual[label] = ["query-timeout"]
                continue
            if query_result.returncode != 0:
                residual[label] = [f"query-exit-{query_result.returncode}"]
                continue
            identifiers = [line for line in query_result.stdout.splitlines() if line]
            if identifiers:
                residual[label] = [backend_image] if label == "images" else identifiers
        if not residual:
            return (
                True,
                output
                + '\npost_cleanup={"containers":0,"networks":0,"volumes":0,"images":0}',
            )
        for label in ("containers", "networks", "volumes"):
            for identifier in residual.get(label, []):
                if identifier.startswith("query-"):
                    continue
                try:
                    timeout = cleanup_timeout()
                    label_result = subprocess.run(  # noqa: S603
                        [*label_formats[label], identifier],
                        cwd=ROOT,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=timeout,
                    )
                except (HarnessError, subprocess.TimeoutExpired):
                    continue
                if (
                    label_result.returncode != 0
                    or label_result.stdout.strip() != project
                ):
                    continue
                try:
                    timeout = cleanup_timeout()
                    subprocess.run(  # noqa: S603
                        [*removals[label], identifier],
                        cwd=ROOT,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=timeout,
                    )
                except (HarnessError, subprocess.TimeoutExpired):
                    continue
        if residual.get("images") == [backend_image] and "containers" not in residual:
            try:
                timeout = cleanup_timeout()
                subprocess.run(  # noqa: S603
                    ["docker", "image", "rm", backend_image],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout,
                )
            except (HarnessError, subprocess.TimeoutExpired):
                pass
        time.sleep(0.2)

    return False, output + "\npost_cleanup_residual=" + json.dumps(
        residual, sort_keys=True
    )


def main() -> int:
    project = f"engram-d1-fault-{secrets.token_hex(8)}"
    api_key = f"egk_d1_{secrets.token_urlsafe(32)}"
    agent_key = f"egk_d1_agent_{secrets.token_urlsafe(32)}"
    generated_secrets = [api_key, agent_key]
    validate_project_name(project)
    if not COMPOSE_FILE.is_file() or not ENV_EXAMPLE.is_file():
        raise HarnessError("Compose contract or .env.example is missing")
    failed = True
    cleanup_ok = False
    cleanup_output = ""
    harness: Harness | None = None
    with tempfile.TemporaryDirectory(prefix="engram-d1-fault-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        env_file = (temp_dir / "generated.env").resolve()
        shutil.copyfile(ENV_EXAMPLE, env_file)
        override_file = (temp_dir / "ports.override.yml").resolve()
        write_port_override(override_file, project)
        config_dir = temp_dir / "cli"
        config_dir.mkdir()
        harness = Harness(
            project=project,
            env_file=env_file,
            override_file=override_file,
            generated_secrets=generated_secrets,
        )
        progress(
            f"project={project} global_deadline={GLOBAL_TIMEOUT:.0f}s "
            f"workload_budget={WORKLOAD_TIMEOUT:.0f}s "
            f"reserved_tail={POST_WORKLOAD_RESERVE:.0f}s"
        )
        try:
            harness.compose(
                "up",
                "-d",
                "--build",
                "--wait",
                "api",
                "worker-near-realtime",
                "relay",
                "beat",
                timeout=600,
            )
            harness.refresh_api_origin()
            bootstrap = harness.compose_json(
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
                "--json",
            )
            project_id = required_string(bootstrap, "project_id")
            connect_cli(harness, config_dir, api_key, bootstrap)
            run_prefix = secrets.token_hex(5)
            harness.phase(
                "Fault A Rabbit ready-delivery recreation",
                lambda: fault_a(harness, config_dir, project_id, f"{run_prefix}-a"),
            )
            harness.phase(
                "Fault B broker reconnect",
                lambda: fault_b(harness, config_dir, project_id, f"{run_prefix}-b"),
            )
            harness.phase("Fault C Beat cursor recreation", lambda: fault_c(harness))
            harness.phase(
                "Fault D PID1 SIGTERM delivery",
                lambda: fault_d(
                    harness,
                    config_dir,
                    project_id,
                    f"{run_prefix}-d-relay",
                    api_key=api_key,
                    bootstrap=bootstrap,
                ),
            )
            failed = False
        except HarnessError as error:
            progress(f"failure: {redact_secrets(str(error), generated_secrets)}")
            harness.dump_failure_logs()
        finally:
            cleanup_ok, cleanup_output = cleanup(project, env_file, generated_secrets)
            progress(f"cleanup_ok={cleanup_ok}")
            if cleanup_output:
                progress(f"cleanup tail:\n{cleanup_output}")
            elapsed = time.monotonic() - harness.started_at
            progress(
                f"phase_timings={json.dumps(harness.phase_timings, sort_keys=True)} total={elapsed:.1f}s"
            )
    if failed or not cleanup_ok:
        return 1

    progress("all disposable runtime durability faults passed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import cast

import pytest
import yaml

ComposeMapping = dict[str, object]

WORKER_QUEUES = {
    'worker-realtime': 'engram-realtime',
    'worker-near-realtime': 'engram-near-realtime',
    'worker-batch': 'engram-batch',
    'worker-highmemory': 'engram-highmemory',
    'worker-domain-events': 'engram-domain-events',
}
WORKER_SERVICES = tuple(WORKER_QUEUES)
BACKEND_SERVICES = ('api', *WORKER_SERVICES, 'beat', 'relay')
LONG_LIVED_SERVICES = (*BACKEND_SERVICES, 'rabbitmq', 'redis', 'postgres', 'frontend')
FRONTEND_CI_STEPS = (
    'pnpm typecheck',
    'pnpm lint',
    'pnpm build',
    'node --test src/lib/memory-conflict-actions.test.ts',
)

_DURATION_PART = re.compile(r'(?P<value>\d+(?:\.\d+)?)(?P<unit>ns|us|ms|s|m|h)')
_DURATION_UNIT_SECONDS = {
    'ns': 0.000000001,
    'us': 0.000001,
    'ms': 0.001,
    's': 1,
    'm': 60,
    'h': 3600,
}
_COMPOSE_DEFAULT = re.compile(r'^\$\{(?P<name>[A-Z0-9_]+):-(?P<default>[^}]*)}$')


def _as_mapping(value: object, *, label: str) -> ComposeMapping:
    assert isinstance(value, dict), f'{label} must be a mapping'

    return cast(ComposeMapping, value)


def _load_compose_contract() -> ComposeMapping:
    configured_path = os.environ.get('ENGRAM_COMPOSE_CONTRACT_PATH')
    contract_path = (
        Path(configured_path)
        if configured_path
        else Path(__file__).resolve().parents[4] / 'deploy' / 'compose' / 'docker-compose.yml'
    )
    assert contract_path.is_file(), f'Compose contract file is absent: {contract_path}'

    config = yaml.safe_load(contract_path.read_text(encoding='utf-8'))

    return _as_mapping(config, label='Compose contract')


@pytest.fixture(scope='module')
def f_compose() -> ComposeMapping:
    return _load_compose_contract()


def _service(config: ComposeMapping, name: str) -> ComposeMapping:
    services = _as_mapping(config.get('services'), label='services')
    assert name in services, f'service is absent: {name}'

    return _as_mapping(services[name], label=f'service {name}')


def _duration_seconds(value: object) -> float:
    assert isinstance(value, str), 'duration must include a unit'
    compact_value = value.strip()
    parts = list(_DURATION_PART.finditer(compact_value))
    assert parts and ''.join(part.group(0) for part in parts) == compact_value, f'invalid duration: {value}'

    return sum(float(part.group('value')) * _DURATION_UNIT_SECONDS[part.group('unit')] for part in parts)


def _list_command(service: ComposeMapping, *, label: str) -> list[str]:
    command = service.get('command')
    assert isinstance(command, list), f'{label} command must use Compose list form'
    assert command and all(isinstance(token, str) for token in command), f'{label} command must contain string tokens'

    return cast(list[str], command)


def _command_tokens(command: object, *, label: str) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)

    assert isinstance(command, list), f'{label} command must be a string or list'
    assert command and all(isinstance(token, str) for token in command), f'{label} command must contain string tokens'

    return cast(list[str], command)


def _command_text(service: ComposeMapping) -> str:
    command = service.get('command')
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        assert all(isinstance(token, str) for token in command)

        return ' '.join(cast(list[str], command))

    assert command is None, 'service command must be a string or list'

    return ''


def _option_integer(command: list[str], option: str) -> int:
    return int(_option_value(command, option))


def _option_value(command: list[str], option: str) -> str:
    values: list[str] = []
    for index, token in enumerate(command):
        if token == option:
            assert index + 1 < len(command), f'{option} requires a value'
            values.append(command[index + 1])
        elif token.startswith(f'{option}='):
            values.append(token.removeprefix(f'{option}='))

    assert len(values) == 1, f'{option} must be specified exactly once'

    return values[0]


def _assert_stop_policy(service: ComposeMapping, expected_seconds: float) -> None:
    assert service.get('stop_signal') == 'SIGTERM'
    assert _duration_seconds(service.get('stop_grace_period')) == expected_seconds


def _volume_mounts(service: ComposeMapping) -> list[tuple[str, str]]:
    volumes = service.get('volumes', [])
    assert isinstance(volumes, list), 'service volumes must be a list'
    mounts: list[tuple[str, str]] = []
    for volume in volumes:
        if isinstance(volume, str):
            parts = volume.split(':')
            assert len(parts) >= 2, f'volume mount has no target: {volume}'
            mounts.append((parts[0], parts[1]))
            continue

        mapping = _as_mapping(volume, label='volume mount')
        source = mapping.get('source')
        target = mapping.get('target')
        assert isinstance(source, str) and isinstance(target, str), 'volume source and target must be strings'
        mounts.append((source, target))

    return mounts


def _named_volume_at(config: ComposeMapping, service_name: str, target: str) -> str:
    matching_sources = [
        source for source, mount_target in _volume_mounts(_service(config, service_name)) if mount_target == target
    ]
    assert len(matching_sources) == 1, f'{service_name} must mount exactly one volume at {target}'
    source = matching_sources[0]
    top_level_volumes = _as_mapping(config.get('volumes'), label='top-level volumes')
    assert source in top_level_volumes, f'{source} must be a declared named volume'

    return source


def _compose_default(value: object, *, variable: str) -> str:
    assert isinstance(value, str), f'{variable} interpolation must be a string'
    match = _COMPOSE_DEFAULT.fullmatch(value)
    assert match is not None and match.group('name') == variable, (
        f'{variable} must use default-preserving interpolation'
    )

    return match.group('default')


def test_api_bootstrap_execs_granian_with_sufficient_grace(f_compose: ComposeMapping) -> None:
    api = _service(f_compose, 'api')
    command = _command_tokens(api.get('command'), label='api')

    assert command[0] in {'sh', '/bin/sh'}
    assert command[1] == '-ec'
    assert len(command) == 3
    bootstrap = command[2]
    final_clause = re.split(r'\s*&&\s*', bootstrap)[-1]
    final_tokens = shlex.split(final_clause)
    assert final_tokens[:2] == ['exec', 'granian']
    assert not re.search(r'(?:&&|\|\||[;|])', final_clause)
    assert bootstrap.count('exec granian') == 1
    exec_suffix = bootstrap[bootstrap.index('exec granian') :]
    assert not re.search(r'(^|[;&|]\s*)(?:/bin/)?(?:ba|da|a|z|k)?sh\b', exec_suffix)

    _assert_stop_policy(api, 45)
    environment = _as_mapping(api.get('environment'), label='api environment')
    worker_kill_timeout = int(cast(str, environment.get('GRANIAN_WORKERS_KILL_TIMEOUT')))
    assert worker_kill_timeout == 35
    assert _duration_seconds(api.get('stop_grace_period')) > worker_kill_timeout


def test_all_workers_are_direct_commands_with_exact_stop_policy(f_compose: ComposeMapping) -> None:
    for service_name, queue_name in WORKER_QUEUES.items():
        worker = _service(f_compose, service_name)
        command = _list_command(worker, label=service_name)

        assert command[:4] == ['celery', '-A', 'engram.celery_app', 'worker']
        assert _option_value(command, '-Q') == queue_name
        prefetch_options = [token for token in command if token.startswith('--prefetch-multiplier')]
        if service_name == 'worker-batch':
            assert prefetch_options == ['--prefetch-multiplier=1']
        else:
            assert prefetch_options == []

        _assert_stop_policy(worker, 12 * 60)


def test_relay_is_direct_and_its_timeouts_fit_inside_grace(f_compose: ComposeMapping) -> None:
    relay = _service(f_compose, 'relay')
    command = _list_command(relay, label='relay')

    assert command[:3] == ['python', 'manage.py', 'celery_outbox_relay']
    shutdown_timeout = _option_integer(command, '--shutdown-timeout')
    send_timeout = _option_integer(command, '--send-timeout')
    relay_grace = _duration_seconds(relay.get('stop_grace_period'))
    assert shutdown_timeout == 45
    assert send_timeout == 10
    assert shutdown_timeout + send_timeout < relay_grace
    _assert_stop_policy(relay, 60)


def test_beat_has_one_direct_process_and_durable_schedule(f_compose: ComposeMapping) -> None:
    services = _as_mapping(f_compose.get('services'), label='services')
    beat_services = [
        service_name
        for service_name, value in services.items()
        if re.search(r'\bcelery\b.*\bbeat\b', _command_text(_as_mapping(value, label=f'service {service_name}')))
    ]
    assert beat_services == ['beat']

    beat = _service(f_compose, 'beat')
    command = _list_command(beat, label='beat')
    assert command[:4] == ['celery', '-A', 'engram.celery_app', 'beat']
    schedule_path = Path(_option_value(command, '--schedule'))
    assert schedule_path == Path('/var/lib/engram-beat/celerybeat-schedule')
    _named_volume_at(f_compose, 'beat', '/var/lib/engram-beat')
    _assert_stop_policy(beat, 30)
    assert beat.get('restart') == 'unless-stopped'


def test_rabbitmq_has_stable_identity_durable_state_and_application_readiness(
    f_compose: ComposeMapping,
) -> None:
    rabbitmq = _service(f_compose, 'rabbitmq')
    hostname = _compose_default(rabbitmq.get('hostname'), variable='ENGRAM_RABBITMQ_HOSTNAME')
    environment = _as_mapping(rabbitmq.get('environment'), label='rabbitmq environment')
    nodename = _compose_default(environment.get('RABBITMQ_NODENAME'), variable='ENGRAM_RABBITMQ_NODENAME')

    assert nodename == f'rabbit@{hostname}'
    assert hostname == 'rabbitmq'
    _named_volume_at(f_compose, 'rabbitmq', '/var/lib/rabbitmq')
    assert rabbitmq.get('restart') == 'unless-stopped'

    healthcheck = _as_mapping(rabbitmq.get('healthcheck'), label='rabbitmq healthcheck')
    healthcheck_test = healthcheck.get('test')
    assert isinstance(healthcheck_test, list) and all(isinstance(token, str) for token in healthcheck_test)
    healthcheck_tokens = cast(list[str], healthcheck_test)
    assert len(healthcheck_tokens) == 2
    assert healthcheck_tokens[0] == 'CMD-SHELL'
    healthcheck_command = ' '.join(healthcheck_tokens[1].split()).replace('> /dev/null', '>/dev/null')
    assert healthcheck_command == (
        'rabbitmq-diagnostics -q ping && rabbitmqctl -q -p engram list_queues name >/dev/null'
    )


def test_all_long_lived_services_restart_and_backend_env_is_overrideable(f_compose: ComposeMapping) -> None:
    for service_name in LONG_LIVED_SERVICES:
        restart_policy = _service(f_compose, service_name).get('restart')
        assert restart_policy == 'unless-stopped'

    for service_name in BACKEND_SERVICES:
        env_file = _service(f_compose, service_name).get('env_file')
        assert env_file == ['${ENGRAM_ENV_FILE:-.env}']


def test_frontend_ci_target_runs_the_cp5_conflict_frontend_gate(f_compose: ComposeMapping) -> None:
    service = _service(f_compose, 'frontend-ci')
    build = _as_mapping(service.get('build'), label='frontend-ci build')
    assert build.get('target') == 'builder'

    assert service.get('profiles') == ['ci']
    assert 'frontend-ci' not in LONG_LIVED_SERVICES
    assert service.get('restart') is None

    command = _command_tokens(service.get('command'), label='frontend-ci')
    assert command[:2] in (['sh', '-ec'], ['/bin/sh', '-ec'])
    assert len(command) == 3
    script = command[2]
    for step in FRONTEND_CI_STEPS:
        assert step in script, f'frontend-ci gate is missing step: {step}'

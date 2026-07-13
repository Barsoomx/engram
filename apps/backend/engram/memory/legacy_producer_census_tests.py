from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from engram.memory import tasks as memory_tasks

_LEGACY_DIGEST_CALLS = frozenset(
    {
        ('generate_daily_digest', 'delay'),
        ('generate_daily_digest', 'apply_async'),
        ('generate_weekly_digest', 'delay'),
        ('generate_weekly_digest', 'apply_async'),
    }
)
_LEGACY_SESSION_CALLS = frozenset(
    {
        ('distill_session', 'delay'),
        ('distill_session', 'apply_async'),
    }
)
_RETAINED_LEGACY_TASK_DEFINITIONS = ('generate_daily_digest', 'generate_weekly_digest', 'distill_session')
_LEGACY_TASK_NAMES = frozenset(
    {
        'engram.memory.generate_daily_digest',
        'engram.memory.generate_weekly_digest',
        'engram.memory.distill_session',
    }
)
_DISPATCH_BY_NAME_FUNCS = frozenset({'send_task', 'signature'})


def _call_path(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)

    return tuple(reversed(parts))


def _is_legacy_digest_call(node: ast.Call) -> bool:
    return _call_path(node.func)[-2:] in _LEGACY_DIGEST_CALLS


def _is_legacy_session_call(node: ast.Call) -> bool:
    return _call_path(node.func)[-2:] in _LEGACY_SESSION_CALLS


def _is_legacy_dispatch_name_call(node: ast.Call) -> bool:
    path = _call_path(node.func)
    if not path or path[-1] not in _DISPATCH_BY_NAME_FUNCS:
        return False
    constants = [argument for argument in node.args if isinstance(argument, ast.Constant)]
    constants += [keyword.value for keyword in node.keywords if isinstance(keyword.value, ast.Constant)]

    return any(constant.value in _LEGACY_TASK_NAMES for constant in constants)


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node

    raise AssertionError(f'function {name} not found')


def _production_trees() -> tuple[Path, list[tuple[Path, ast.Module]]]:
    package_root = Path(inspect.getfile(memory_tasks)).parents[1]
    trees = [
        (path, ast.parse(path.read_text(encoding='utf-8')))
        for path in sorted(package_root.rglob('*.py'))
        if not path.name.endswith('_tests.py')
    ]

    return package_root, trees


def _legacy_call_sites(predicate: object) -> list[tuple[str, int]]:
    package_root, production_trees = _production_trees()

    return [
        (str(path.relative_to(package_root)), node.lineno)
        for path, tree in production_trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and predicate(node)
    ]


@pytest.mark.parametrize(
    'source',
    (
        'generate_daily_digest.delay()',
        'generate_daily_digest.apply_async()',
        'generate_weekly_digest.delay()',
        'generate_weekly_digest.apply_async()',
        'tasks.generate_daily_digest.delay()',
    ),
)
def test_legacy_digest_call_census_recognizes_all_call_forms(source: str) -> None:
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert [node for node in calls if _is_legacy_digest_call(node)]


def test_legacy_digest_call_census_ignores_work_v1_signals() -> None:
    source = 'generate_daily_digest_work_v1.apply_async(); dispatch_work_task(task, work_id)'
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert [node for node in calls if _is_legacy_digest_call(node)] == []


def test_final_census_has_zero_active_legacy_digest_producer_calls() -> None:
    assert _legacy_call_sites(_is_legacy_digest_call) == []


def test_final_census_has_zero_active_legacy_session_producer_calls() -> None:
    assert _legacy_call_sites(_is_legacy_session_call) == []


def test_retained_legacy_task_definitions_are_still_present() -> None:
    tasks_tree = ast.parse(Path(inspect.getfile(memory_tasks)).read_text(encoding='utf-8'))
    defined = {node.name for node in ast.walk(tasks_tree) if isinstance(node, ast.FunctionDef)}
    for name in _RETAINED_LEGACY_TASK_DEFINITIONS:
        assert name in defined


def test_scheduled_producer_tasks_carry_no_legacy_digest_call() -> None:
    tasks_tree = ast.parse(Path(inspect.getfile(memory_tasks)).read_text(encoding='utf-8'))
    for task_name in ('run_scheduled_digests', 'run_scheduled_weekly_digests'):
        function = _function_def(tasks_tree, task_name)
        legacy = [node for node in ast.walk(function) if isinstance(node, ast.Call) and _is_legacy_digest_call(node)]
        assert legacy == [], task_name


@pytest.mark.parametrize(
    'source',
    (
        "app.send_task('engram.memory.generate_daily_digest', args=[work_id])",
        "app.send_task('engram.memory.generate_weekly_digest')",
        "app.send_task(name='engram.memory.distill_session')",
        "signature('engram.memory.generate_daily_digest')",
    ),
)
def test_legacy_dispatch_name_census_recognizes_send_task_and_signature_forms(source: str) -> None:
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert [node for node in calls if _is_legacy_dispatch_name_call(node)]


def test_legacy_dispatch_name_census_ignores_work_v1_task_names() -> None:
    source = "app.send_task('engram.memory.distill_session_work_v1', args=[work_id])"
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert [node for node in calls if _is_legacy_dispatch_name_call(node)] == []


def test_final_census_has_zero_legacy_task_name_dispatches() -> None:
    assert _legacy_call_sites(_is_legacy_dispatch_name_call) == []

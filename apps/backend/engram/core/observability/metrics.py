from __future__ import annotations

import threading
from collections import defaultdict


def _escape_label_value(value: str) -> str:
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ''

    pairs = [f'{key}="{_escape_label_value(val)}"' for key, val in sorted(labels.items())]

    return '{' + ','.join(pairs) + '}'


def _format_counter_line(name: str, labels: dict[str, str], value: float) -> str:
    return f'{name}{_format_labels(labels)} {value}'


class Counter:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self._name = name
        self._help_text = help_text
        self._label_names = label_names
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def help_text(self) -> str:
        return self._help_text

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names

    def inc(self, value: float = 1.0, **labels: str) -> None:
        if set(labels.keys()) != set(self._label_names):
            raise ValueError(
                f'counter {self._name} expects labels {self._label_names}, got {tuple(labels.keys())}',
            )

        key = tuple(labels[name] for name in self._label_names)
        with self._lock:
            self._values[key] += value

    def value(self, **labels: str) -> float:
        key = tuple(labels[name] for name in self._label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def samples(self) -> list[tuple[dict[str, str], float]]:
        with self._lock:
            return [(dict(zip(self._label_names, key, strict=False)), count) for key, count in self._values.items()]

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


def render_prometheus(counters: list[Counter]) -> str:
    lines: list[str] = []

    for counter in counters:
        lines.append(f'# HELP {counter.name} {counter.help_text}')
        lines.append(f'# TYPE {counter.name} counter')

        for labels, value in counter.samples():
            lines.append(_format_counter_line(counter.name, labels, value))

    return '\n'.join(lines) + '\n' if lines else ''

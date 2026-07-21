from __future__ import annotations

from django.core.management import get_commands


def test_cp5_curator_eval_command_is_registered() -> None:
    assert get_commands().get('engram_curator_eval') == 'engram.core'

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.memory.evals.scorer import load_responses, score_corpus


class Command(BaseCommand):
    help = 'Score the CP5 curation corpus against frozen quality thresholds and exit nonzero on a miss.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--engine', choices=('fixture', 'responses'), default='fixture', dest='engine')
        parser.add_argument('--responses', default=None, dest='responses')
        parser.add_argument('--format', choices=('json',), default='json', dest='report_format')

    def handle(self, *args: Any, **options: Any) -> None:
        responses_path = options['responses']
        engine = 'responses' if responses_path else str(options['engine'])
        if engine == 'responses' and not responses_path:
            raise CommandError('responses engine requires --responses <jsonl>')

        responses = load_responses(Path(responses_path)) if responses_path else None
        report = score_corpus(engine=engine, responses=responses)

        self.stdout.write(json.dumps(report, sort_keys=True, indent=2))

        if not report['passed']:
            failed = [check['metric'] for check in report['thresholds'] if not check['passed']]
            raise CommandError(f'curator eval threshold miss: {",".join(failed)}')

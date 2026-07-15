from __future__ import annotations

import os

from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.views.decorators.http import require_GET

from engram.core.middlewares.metrics import http_requests_total
from engram.core.observability.metrics import render_prometheus
from engram.memory.metrics import consistency_issues_total, projection_rebuilds_total

CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


@require_GET
def metrics(request: HttpRequest) -> HttpResponse:
    expected_token = os.environ.get('ENGRAM_METRICS_TOKEN', '')
    if expected_token:
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header != f'Bearer {expected_token}':
            return HttpResponseForbidden('Forbidden')

    body = render_prometheus(
        [
            http_requests_total,
            consistency_issues_total,
            projection_rebuilds_total,
        ]
    )

    return HttpResponse(
        body,
        content_type=CONTENT_TYPE,
    )

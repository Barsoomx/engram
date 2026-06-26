from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_GET

from engram.core.middlewares.metrics import http_requests_total
from engram.core.observability.metrics import render_prometheus

CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


@require_GET
def metrics(_request: HttpRequest) -> HttpResponse:
    body = render_prometheus([http_requests_total])

    return HttpResponse(
        body,
        content_type=CONTENT_TYPE,
    )

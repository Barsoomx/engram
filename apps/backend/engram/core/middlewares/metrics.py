from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from engram.core.observability.metrics import Counter

HTTP_REQUESTS_COUNTER_NAME = 'engram_http_requests_total'


http_requests_total = Counter(
    name=HTTP_REQUESTS_COUNTER_NAME,
    help_text='Total number of HTTP requests processed, labeled by view and status class.',
    label_names=('view', 'status'),
)


def status_class(status_code: int) -> str:
    return f'{status_code // 100}xx'


class MetricsMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self._get_response(request)

        view = self._resolve_view_name(request)
        http_requests_total.inc(view=view, status=status_class(response.status_code))

        return response

    def _resolve_view_name(self, request: HttpRequest) -> str:
        match = request.resolver_match

        if match is not None:
            view_name = match.view_name or match.url_name
            if view_name:
                return view_name

        return request.path

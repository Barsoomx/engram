from collections.abc import Callable

from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpRequest, HttpResponse, JsonResponse

from engram.core.domain.usecases.errors import DomainError
from engram.core.middlewares.drf_exception_handler import build_domain_error_payload


class ExceptionHandlingMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: WSGIRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(
        self,
        request: HttpRequest,
        exception: Exception,
    ) -> HttpResponse | None:
        if isinstance(exception, DomainError):
            response_data = build_domain_error_payload(exception)
            return JsonResponse(
                response_data,
                status=exception.status_code,
            )

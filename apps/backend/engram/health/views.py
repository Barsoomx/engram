from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(_request: object) -> JsonResponse:
    return JsonResponse(
        {
            'status': 'ok',
            'checks': {'process': 'ok'},
        },
    )


@require_GET
def readyz(_request: object) -> JsonResponse:
    return database_status_response()


@require_GET
def startup(_request: object) -> JsonResponse:
    return database_status_response()


def database_status_response() -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except DatabaseError:
        return JsonResponse(
            {
                'status': 'unavailable',
                'checks': {'database': 'unavailable'},
            },
            status=503,
        )

    return JsonResponse(
        {
            'status': 'ok',
            'checks': {'database': 'ok'},
        },
    )

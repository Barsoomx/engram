from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from engram.core.api.pagination import PageNumberPageSizePagination


def _request(query: str) -> Request:
    return Request(APIRequestFactory().get(f'/{query}'))


def test_page_size_query_param_is_honored() -> None:
    paginator = PageNumberPageSizePagination()

    page = paginator.paginate_queryset(list(range(50)), _request('?page_size=5'))

    assert len(page) == 5


def test_page_size_is_capped_at_max() -> None:
    paginator = PageNumberPageSizePagination()

    page = paginator.paginate_queryset(list(range(500)), _request('?page_size=999'))

    assert len(page) == 100


def test_default_page_size_when_param_absent() -> None:
    paginator = PageNumberPageSizePagination()

    page = paginator.paginate_queryset(list(range(50)), _request(''))

    assert len(page) == 20

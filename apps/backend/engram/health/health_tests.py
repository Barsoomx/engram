import pytest
from django.test import Client


def test_healthz_returns_process_status(client: Client) -> None:
    response = client.get('/-/healthz/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'process': 'ok'},
    }


@pytest.mark.django_db
def test_readyz_checks_database(client: Client) -> None:
    response = client.get('/-/readyz/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'database': 'ok'},
    }


@pytest.mark.django_db
def test_startup_checks_database(client: Client) -> None:
    response = client.get('/-/startup/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'database': 'ok'},
    }

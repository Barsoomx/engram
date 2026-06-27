from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType
from engram.core.models import Organization

LOGIN_URL = '/v1/auth/login'
ME_URL = '/v1/auth/me'
LOGOUT_URL = '/v1/auth/logout'


def create_user(
    *,
    username: str = 'alice',
    password: str = 'strong-secret-123',  # noqa: S107
    is_active: bool = True,
) -> User:
    return User.objects.create_user(
        username=username,
        password=password,
        is_active=is_active,
    )


def auth_client(token: str) -> APIClient:
    client = APIClient()

    client.credentials(HTTP_AUTHORIZATION=f'Token {token}')

    return client


@pytest.mark.django_db
def test_login_with_valid_credentials_returns_token_and_user() -> None:
    user = create_user()

    client = APIClient()
    response = client.post(
        LOGIN_URL,
        data={'username': user.get_username(), 'password': 'strong-secret-123'},
        format='json',
    )

    assert response.status_code == 200

    payload = response.json()
    assert payload['token']
    assert payload['user_id'] == user.id
    assert payload['username'] == user.get_username()

    identity = Identity.objects.get(external_id=external_id_for_user(user))
    assert payload['identity_id'] == str(identity.id)

    organization = Organization.objects.get(slug='default')
    assert payload['organization_id'] == str(organization.id)

    assert 'observations:write' in payload['capabilities']


@pytest.mark.django_db
def test_login_with_invalid_credentials_returns_401() -> None:
    create_user()

    client = APIClient()
    response = client.post(
        LOGIN_URL,
        data={'username': 'alice', 'password': 'wrong-password'},
        format='json',
    )

    assert response.status_code == 401

    payload = response.json()
    assert payload['code'] == 'invalid_credentials'


@pytest.mark.django_db
def test_login_creates_identity_once_and_is_idempotent() -> None:
    user = create_user()

    client = APIClient()
    first = client.post(
        LOGIN_URL,
        data={'username': user.get_username(), 'password': 'strong-secret-123'},
        format='json',
    )
    second = client.post(
        LOGIN_URL,
        data={'username': user.get_username(), 'password': 'strong-secret-123'},
        format='json',
    )

    assert first.status_code == 200
    assert second.status_code == 200

    identities = Identity.objects.filter(identity_type=IdentityType.USER)
    assert identities.count() == 1


@pytest.mark.django_db
def test_me_with_valid_token_returns_user_and_scope() -> None:
    user = create_user()

    client = APIClient()
    login = client.post(
        LOGIN_URL,
        data={'username': user.get_username(), 'password': 'strong-secret-123'},
        format='json',
    )
    token = login.json()['token']

    response = auth_client(token).get(ME_URL)

    assert response.status_code == 200

    payload = response.json()
    assert payload['user_id'] == user.id
    assert payload['username'] == user.get_username()
    assert payload['organization_id']
    assert 'memories:read' in payload['capabilities']


@pytest.mark.django_db
def test_me_without_token_returns_401() -> None:
    response = APIClient().get(ME_URL)

    assert response.status_code == 401


@pytest.mark.django_db
def test_me_with_invalid_token_returns_401() -> None:
    response = auth_client('not-a-real-token').get(ME_URL)

    assert response.status_code == 401


@pytest.mark.django_db
def test_logout_deletes_token() -> None:
    user = create_user()

    client = APIClient()
    login = client.post(
        LOGIN_URL,
        data={'username': user.get_username(), 'password': 'strong-secret-123'},
        format='json',
    )
    token = login.json()['token']

    logout = auth_client(token).post(LOGOUT_URL)

    assert logout.status_code == 204

    me_after = auth_client(token).get(ME_URL)
    assert me_after.status_code == 401


@pytest.mark.django_db
def test_logout_without_token_returns_401() -> None:
    response = APIClient().post(LOGOUT_URL)

    assert response.status_code == 401

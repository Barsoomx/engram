import pytest

from engram.console.exceptions import DigestNotFoundError
from engram.console.exceptions import EmbeddingFieldsRequiredError
from engram.console.exceptions import EmbeddingSecretNotFoundError
from engram.console.exceptions import InvalidRerunSnapshotError
from engram.console.exceptions import MemberAlreadyInvitedError
from engram.console.exceptions import ProjectSlugTakenError
from engram.console.exceptions import TeamSlugTakenError
from engram.core.domain.usecases.errors import DomainError


@pytest.mark.parametrize(
    ('error_cls', 'expected_error_code', 'expected_status_code'),
    [
        (TeamSlugTakenError, 'team_slug_taken', 409),
        (ProjectSlugTakenError, 'project_slug_taken', 409),
        (MemberAlreadyInvitedError, 'member_already_invited', 409),
        (DigestNotFoundError, 'digest_not_found', 404),
        (InvalidRerunSnapshotError, 'invalid_rerun_snapshot', 400),
        (EmbeddingFieldsRequiredError, 'embedding_fields_required', 400),
        (EmbeddingSecretNotFoundError, 'embedding_secret_not_found', 400),
    ],
)
def test_console_domain_error_defaults(
    error_cls: type[DomainError],
    expected_error_code: str,
    expected_status_code: int,
) -> None:
    exc = error_cls('boom')

    assert isinstance(exc, DomainError)
    assert exc.error_code == expected_error_code
    assert exc.status_code == expected_status_code

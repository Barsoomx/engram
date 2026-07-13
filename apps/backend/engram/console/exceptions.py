from __future__ import annotations

from engram.core.domain.usecases.errors import DomainError


class LastOwnerError(DomainError):
    default_error_code = 'last_owner'
    default_status_code = 409


class TeamSlugTakenError(DomainError):
    default_error_code = 'team_slug_taken'
    default_status_code = 409


class ProjectSlugTakenError(DomainError):
    default_error_code = 'project_slug_taken'
    default_status_code = 409


class MemberAlreadyInvitedError(DomainError):
    default_error_code = 'member_already_invited'
    default_status_code = 409


class DigestNotFoundError(DomainError):
    default_error_code = 'digest_not_found'
    default_status_code = 404


class InvalidRerunSnapshotError(DomainError):
    default_error_code = 'invalid_rerun_snapshot'
    default_status_code = 400


class EmbeddingFieldsRequiredError(DomainError):
    default_error_code = 'embedding_fields_required'
    default_status_code = 400


class EmbeddingSecretNotFoundError(DomainError):
    default_error_code = 'embedding_secret_not_found'
    default_status_code = 400


class DailyDigestAlreadyRunningError(DomainError):
    default_error_code = 'daily_digest_already_running'
    default_status_code = 409


class LegacyWorkUnlinkedError(DomainError):
    default_error_code = 'legacy_work_unlinked'
    default_status_code = 409


class WorkflowRunNotTerminalError(DomainError):
    default_error_code = 'workflow_run_not_terminal'
    default_status_code = 409

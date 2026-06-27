from typing import TYPE_CHECKING, Any, Optional

import sentry_sdk
import structlog

from engram.core.domain.event_dispatcher import get_dispatcher
from engram.core.domain.types import TransactionContext
from engram.core.domain.usecases.base import BaseUseCase, TInput, TOutput  # noqa: F401
from engram.core.domain.usecases.errors import DomainError

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = structlog.get_logger(__name__)


class UseCaseTransactional[TInput, TOutput](BaseUseCase[TInput, TOutput]):
    def __init__(
        self,
        user: Optional['AbstractBaseUser'],
        transaction: TransactionContext,
    ) -> None:
        super().__init__(user=user)
        self.__transaction = transaction

    def _get_context_vars(self) -> dict[str, Any]:
        _contextvars = {
            'usecase': self.__class__.__name__,
        }
        if self._user is not None:
            _contextvars['user_id'] = self._user.pk

        return _contextvars

    def execute(self, input_dto: TInput | None = None) -> TOutput:
        with (
            structlog.contextvars.bound_contextvars(
                **self._get_context_vars(),
            ),
            sentry_sdk.start_span(
                op=f'{self.__class__.__name__}._execute',
                name=f'Run usecase {self.__class__.__name__}',
            ),
        ):
            self._pre_commit(input_dto=input_dto)
            try:
                with self.__transaction:
                    result = self._execute(input_dto)

                self._post_commit(input_dto=input_dto, output_dto=result)
                return result
            except DomainError as e:
                self._handle_domain_exception(e=e, input_dto=input_dto)
                if e.SKIP_LOGGING is False:
                    logger.exception(
                        f'{self.__class__.__name__} exception',
                        exc=e,
                        user=self._user,
                    )
                raise e
            except Exception as e:
                self._handle_exception(e=e, input_dto=input_dto)
                logger.exception(
                    f'{self.__class__.__name__} unexpected exception',
                    exc=e,
                    user=self._user,
                )
                raise e

    def _pre_commit(self, input_dto: TInput | None) -> None:
        pass

    def _execute(self, input_dto: TInput | None) -> TOutput:
        raise NotImplementedError

    def _dispatch_events(self) -> None:
        get_dispatcher().dispatch(self._event_store)

    def _post_commit(
        self,
        input_dto: TInput | None,
        output_dto: TOutput | None,
    ) -> None:
        try:
            self._dispatch_events()
        except Exception as e:
            logger.exception('failed to dispatch domain events', error=e)

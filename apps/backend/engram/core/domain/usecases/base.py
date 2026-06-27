from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional, TypeVar

import sentry_sdk
import structlog
from pydantic import BaseModel, ConfigDict

from engram.core.domain.event_dispatcher import get_dispatcher
from engram.core.domain.event_store import EventStore
from engram.core.domain.usecases.errors import DomainError

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = structlog.get_logger(__name__)


class BaseUseCaseInputDTO(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class BaseUseCaseOutputDTO(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


TInput = TypeVar('TInput', bound=BaseUseCaseInputDTO)
TOutput = TypeVar('TOutput', bound=BaseUseCaseOutputDTO)


class BaseUseCase[TInput, TOutput](ABC):
    def __init__(self, user: Optional['AbstractBaseUser'] = None) -> None:
        self._user = user
        self._event_store = EventStore()

    def _get_context_vars(self) -> dict[str, Any]:
        _contextvars = {
            'usecase': self.__class__.__name__,
        }
        if self._user is not None:
            _contextvars['user_id'] = self._user.pk

        return _contextvars

    @abstractmethod
    def _execute(self, input_dto: TInput | None) -> TOutput:
        raise NotImplementedError

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
            try:
                result = self._execute(input_dto)
                self._dispatch_events()
                return result
            except DomainError as e:
                self._handle_domain_exception(e=e, input_dto=input_dto)
                if e.SKIP_LOGGING is False:
                    logger.exception(
                        f'{self.__class__.__name__} exception',
                        exc=e,
                        client=self._user,
                    )
                raise e
            except Exception as e:
                self._handle_exception(e=e, input_dto=input_dto)
                logger.exception(
                    f'{self.__class__.__name__} unexpected exception',
                    exc=e,
                    client=self._user,
                )
                raise e

    def _dispatch_events(self) -> None:
        try:
            get_dispatcher().dispatch(self._event_store)
        except Exception as e:
            logger.exception('failed to dispatch domain events', error=e)

    def _handle_domain_exception(self, e: Exception, input_dto: TInput | None) -> None:
        del e, input_dto

    def _handle_exception(self, e: Exception, input_dto: TInput | None) -> None:
        del e, input_dto

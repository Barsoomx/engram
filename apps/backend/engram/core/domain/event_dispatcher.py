import typing
from collections import defaultdict
from collections.abc import Callable
from typing import TypeVar

import structlog
from celery import shared_task
from celery.app.task import Task
from django.db import transaction

from engram.core.domain.event_store import EventStore
from engram.core.domain.events import DomainEvent
from engram.core.domain.singleton import Singleton
from engram.core.environment import is_running_with_pytest
from engram.core.retries_checker import CELERY_TASK_MAX_ATTEMPTS, CELERY_TASK_RETRY_DELAY

logger = structlog.get_logger(__name__)

TDomainEvent = TypeVar('TDomainEvent', bound=DomainEvent)
TEventHandler = Callable[[TDomainEvent], None]

DOMAIN_DISPATCHER_CLASS_NAME = 'BaseEventDispatcher' if is_running_with_pytest() else 'CeleryEventDispatcher'
QUEUE_DOMAIN_EVENTS = 'engram-domain-events'


class BaseEventDispatcher(metaclass=Singleton):
    METRIC_EVENT_DISPATCH = 'domain_event_dispatch'
    DISPATCHES_HANDLERS_ON_COMMIT = False

    def __init__(self) -> None:
        self._handlers = defaultdict(set)

    def add_handler(
        self,
        event_type: type[DomainEvent],
        handler: TEventHandler,
        queue: str = '',
    ) -> TEventHandler:
        assert issubclass(event_type, DomainEvent)
        wrapper = self._wrap_handler(
            event_type,
            handler,
            queue,
        )

        self._handlers[event_type].add(wrapper)

        logger.debug(
            'event dispatcher: registered handler',
            name=wrapper.__name__,
            event_type=event_type.__name__,
            dispatcher=self.__class__.__name__,
        )

        return wrapper

    def dispatch(self, event_store: EventStore) -> None:
        for event in event_store.get_events():
            domain_event_json = event.model_dump_json()
            for handler in self._handlers[event.__class__]:
                logger.info(
                    'dispatching event',
                    domain_event_json=domain_event_json,
                    handler_name=handler.__name__,
                    handler_path=handler.__module__,
                )

                self._run_handler(handler, event)

        event_store.clear_events()

    def _wrap_handler(
        self,
        event_type: type[DomainEvent],
        handler: TEventHandler,
        queue: str,
    ) -> TEventHandler:
        return handler

    def _run_handler(self, handler: TEventHandler, event: DomainEvent) -> None:
        handler(event)


class CeleryEventDispatcher(BaseEventDispatcher):
    DISPATCHES_HANDLERS_ON_COMMIT = True

    def _wrap_handler(
        self,
        event_type: type[DomainEvent],
        handler: TEventHandler,
        queue: str,
    ) -> TEventHandler:
        def wrapper(
            event: event_type,
        ) -> None:
            logger.info(
                'call domain event handler',
                handler=getattr(handler, '__name__', '-'),
            )

            return handler(typing.cast(TDomainEvent, event))

        wrapper.__name__ = getattr(handler, '__name__', 'domain_event_handler')

        if is_celery_task(handler):
            return handler
        else:
            return shared_task(
                autoretry_for=(Exception,),
                max_retries=CELERY_TASK_MAX_ATTEMPTS,
                default_retry_delay=CELERY_TASK_RETRY_DELAY,
                queue=queue,
                acks_late=True,
                reject_on_worker_lost=True,
                pydantic=True,
            )(wrapper)

    def _run_handler(self, handler: TEventHandler, event: DomainEvent) -> None:
        if transaction.get_connection().in_atomic_block:
            handler.delay_on_commit(event=event.model_dump())
        else:
            handler.delay(event=event.model_dump())


def get_dispatcher() -> BaseEventDispatcher:
    return globals()[DOMAIN_DISPATCHER_CLASS_NAME]()


def dispatch_domain_event(event: DomainEvent) -> None:
    event_store = EventStore()
    event_store.add_event(event)
    get_dispatcher().dispatch(event_store)


class DomainEventHandlerDecorator:
    def __init__(
        self,
        event_type: type[DomainEvent],
        queue: str = QUEUE_DOMAIN_EVENTS,
    ) -> None:
        self._event_type = event_type
        self._queue = queue

    def __call__(self, handler: TEventHandler) -> TEventHandler:
        return get_dispatcher().add_handler(
            event_type=self._event_type,
            handler=handler,
            queue=self._queue,
        )


domain_event_handler = DomainEventHandlerDecorator


def is_celery_task(func: Callable) -> bool:
    return isinstance(func, Task)

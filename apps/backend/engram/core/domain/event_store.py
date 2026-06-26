from engram.core.domain.events import DomainEvent


class EventStore:
    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def add_event(self, event: DomainEvent) -> None:
        self._events.append(event)

    def get_events(self) -> list[DomainEvent]:
        return self._events

    def clear_events(self) -> None:
        self._events.clear()

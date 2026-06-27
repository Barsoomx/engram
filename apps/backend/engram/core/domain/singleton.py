from typing import Any, Self


class Singleton(type):
    _instances: dict['Singleton', Any] = {}

    def __call__(cls, *args: Any, **kwargs: Any) -> Self:
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)

        return cls._instances[cls]

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from urllib.parse import urlparse

from django.conf import settings

from engram.core.environments import is_non_production

INSECURE_OVERRIDE_ENV = 'ENGRAM_ALLOW_INSECURE_PROVIDER_URLS'
_TRUTHY = frozenset({'1', 'true', 'yes', 'on', 'enabled'})

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], tuple[str, ...]]


class BaseUrlValidationError(ValueError):
    def __init__(self, public_message: str) -> None:
        self.public_message = public_message

        super().__init__(public_message)


def _default_resolver(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, None)

    return tuple({info[4][0] for info in infos})


def _environment() -> str:
    return getattr(settings, 'ENVIRONMENT', 'dev')


def _insecure_override_enabled() -> bool:
    return str(os.environ.get(INSECURE_OVERRIDE_ENV, '')).strip().casefold() in _TRUTHY


def _as_ip_literal(host: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_blocked_address(ip: IpAddress, *, allow_loopback: bool) -> bool:
    if ip.is_loopback or ip.is_unspecified:
        return not allow_loopback

    return ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast


def _check_scheme(scheme: str, *, relaxed: bool) -> None:
    if scheme not in {'http', 'https'}:
        raise BaseUrlValidationError('base_url must use http or https')

    if scheme != 'https' and not relaxed:
        raise BaseUrlValidationError('base_url must use https')


def _resolve_addresses(host: str, resolver: Resolver | None) -> tuple[IpAddress, ...]:
    literal = _as_ip_literal(host)
    if literal is not None:
        return (literal,)

    resolve = resolver or _default_resolver
    try:
        resolved = resolve(host)
    except OSError as error:
        raise BaseUrlValidationError('base_url host could not be resolved') from error

    addresses = tuple(ipaddress.ip_address(value) for value in resolved)
    if not addresses:
        raise BaseUrlValidationError('base_url host could not be resolved')

    return addresses


def validate_base_url(base_url: str, *, resolver: Resolver | None = None) -> None:
    if not base_url:
        return

    dev_or_test = is_non_production(_environment())
    override = _insecure_override_enabled()

    parsed = urlparse(base_url)
    _check_scheme((parsed.scheme or '').casefold(), relaxed=dev_or_test or override)

    host = parsed.hostname
    if not host:
        raise BaseUrlValidationError('base_url must include a host')

    if dev_or_test:
        return

    for ip in _resolve_addresses(host, resolver):
        if _is_blocked_address(ip, allow_loopback=override):
            raise BaseUrlValidationError('base_url host resolves to a disallowed address')

    return

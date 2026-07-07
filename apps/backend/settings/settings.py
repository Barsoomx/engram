from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import structlog
from django.core.exceptions import ImproperlyConfigured

from engram.core.environments import is_non_production

from .logs import configure_logger

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_DEV_SECRET_KEY = 'engram-development-secret'


def to_bool(value: str | bool | None) -> bool:
    return str(value).casefold() in {'1', 'true', 'yes', 'on', 'enabled'}


def csv(value: str, *, default: tuple[str, ...]) -> list[str]:
    items = [item.strip() for item in value.split(',') if item.strip()]
    return items or list(default)


def database_config(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme
    if scheme in {'postgres', 'postgresql'}:
        engine = 'django.db.backends.postgresql'
    elif scheme == 'sqlite':
        engine = 'django.db.backends.sqlite3'
    else:
        raise ValueError(f'unsupported database scheme: {scheme}')

    if engine == 'django.db.backends.sqlite3':
        return {
            'ENGINE': engine,
            'NAME': parsed.path.lstrip('/') or ':memory:',
        }

    return {
        'ENGINE': engine,
        'NAME': parsed.path.lstrip('/'),
        'USER': parsed.username or '',
        'PASSWORD': parsed.password or '',
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or 5432),
    }


def require_secret_key(*, raw_secret_key: str, environment: str) -> str:
    if is_non_production(environment):
        return raw_secret_key or DEFAULT_DEV_SECRET_KEY

    if not raw_secret_key or raw_secret_key == DEFAULT_DEV_SECRET_KEY:
        raise ImproperlyConfigured(
            'ENGRAM_SECRET_KEY must be set to a strong non-default value outside dev/test environments',
        )

    return raw_secret_key


def resolve_allowed_hosts(*, raw_allowed_hosts: str, environment: str) -> list[str]:
    if is_non_production(environment):
        return csv(raw_allowed_hosts or 'localhost,127.0.0.1,0.0.0.0', default=('localhost',))

    if not raw_allowed_hosts.strip():
        raise ImproperlyConfigured(
            'ENGRAM_ALLOWED_HOSTS must be set explicitly outside dev/test environments',
        )

    return csv(raw_allowed_hosts, default=('localhost',))


ENVIRONMENT = os.environ.get('ENGRAM_ENVIRONMENT', 'dev')
SECRET_KEY = require_secret_key(raw_secret_key=os.environ.get('ENGRAM_SECRET_KEY', ''), environment=ENVIRONMENT)
ENGRAM_SECRET_ENCRYPTION_KEY = os.environ.get('ENGRAM_SECRET_ENCRYPTION_KEY', '')
DEBUG = to_bool(os.environ.get('ENGRAM_DEBUG', 'false'))
ALLOWED_HOSTS = resolve_allowed_hosts(
    raw_allowed_hosts=os.environ.get('ENGRAM_ALLOWED_HOSTS', ''),
    environment=ENVIRONMENT,
)
ROOT_URLCONF = 'settings.urls'
WSGI_APPLICATION = 'settings.wsgi.application'
ASGI_APPLICATION = 'settings.asgi.application'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
HTTP_HOST = os.getenv('HTTP_HOST', 'http://localhost:8000')
_DEFAULT_BROWSER_ORIGINS = ','.join(
    (
        HTTP_HOST,
        'http://127.0.0.1',
        'http://127.0.0.1:8000',
        'http://127.0.0.1:3000',
        'http://localhost',
        'http://localhost:3000',
        'http://0.0.0.0',
    )
)
CSRF_TRUSTED_ORIGINS = csv(
    os.environ.get('ENGRAM_CSRF_TRUSTED_ORIGINS', _DEFAULT_BROWSER_ORIGINS),
    default=(HTTP_HOST,),
)
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = csv(
    os.environ.get('ENGRAM_CORS_ALLOWED_ORIGINS', _DEFAULT_BROWSER_ORIGINS),
    default=(HTTP_HOST,),
)
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_HEADERS = (
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
    'x-engram-organization',
    'x-engram-project',
    'x-engram-team',
)

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework.authtoken',
    'django_filters',
    'drf_spectacular',
    'corsheaders',
    'django_structlog',
    'django_celery_outbox',
    'engram.core',
    'engram.access',
    'engram.hooks',
    'engram.imports',
    'engram.memory',
    'engram.context',
    'engram.inspection',
    'engram.observations',
    'engram.model_policy',
    'engram.search',
    'engram.health',
    'engram.console',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django_structlog.middlewares.RequestMiddleware',
    'engram.core.middlewares.MetricsMiddleware',
    'engram.core.middlewares.ApiRequestResponseLoggingMiddleware',
    'engram.core.middlewares.ExceptionHandlingMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

DATABASES = {
    'default': {
        **database_config(os.environ.get('ENGRAM_DATABASE_URL', 'postgresql://engram:engram@postgres:5432/engram')),
        'CONN_MAX_AGE': int(os.environ.get('ENGRAM_DATABASE_CONN_MAX_AGE', '60')),
        'CONN_HEALTH_CHECKS': True,
    },
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

ENGRAM_REDIS_URL = os.environ.get('ENGRAM_REDIS_URL', 'redis://redis:6379/0')
ENGRAM_CELERY_BROKER_URL = os.environ.get('ENGRAM_CELERY_BROKER_URL', 'amqp://engram:engram@rabbitmq:5672/engram')
CELERY_BROKER_URL = ENGRAM_CELERY_BROKER_URL
CELERY_RESULT_BACKEND = os.environ.get('ENGRAM_CELERY_RESULT_BACKEND', ENGRAM_REDIS_URL)
CELERY_TASK_IGNORE_RESULT = True
CELERY_OUTBOX_APP = 'engram.celery_app.app'
DJANGO_STRUCTLOG_CELERY_ENABLED = True

ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = Decimal(
    os.environ.get('ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD', '0.500'),
)

ENGRAM_NEAR_DUP_THRESHOLD = Decimal(
    os.environ.get('ENGRAM_NEAR_DUP_THRESHOLD', '0.850'),
)

ENGRAM_CONFIDENCE_DECAY_STEP = Decimal(
    os.environ.get('ENGRAM_CONFIDENCE_DECAY_STEP', '0.050'),
)

ENGRAM_CONFIDENCE_DECAY_FLOOR = Decimal(
    os.environ.get('ENGRAM_CONFIDENCE_DECAY_FLOOR', '0.200'),
)

ENGRAM_CONFIDENCE_DECAY_MIN_AGE_DAYS = int(os.environ.get('ENGRAM_CONFIDENCE_DECAY_MIN_AGE_DAYS', '30'))

ENGRAM_CURATOR_ESCALATION_ENABLED = to_bool(os.environ.get('ENGRAM_CURATOR_ESCALATION_ENABLED', 'true'))

_DEFAULT_CURATOR_SENSITIVE_TERMS = (
    'password',
    'private key',
    'api key',
    'api_key',
    'access key',
    'client secret',
    'secret key',
    'vault',
    'cve-',
    'vulnerability',
    'security incident',
)
ENGRAM_CURATOR_SENSITIVE_TERMS = tuple(
    term.casefold()
    for term in csv(
        os.environ.get('ENGRAM_CURATOR_SENSITIVE_TERMS', ','.join(_DEFAULT_CURATOR_SENSITIVE_TERMS)),
        default=_DEFAULT_CURATOR_SENSITIVE_TERMS,
    )
)

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': ENGRAM_REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
    },
}

REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'engram.core.middlewares.custom_exception_handler',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    'DEFAULT_PAGINATION_CLASS': 'engram.core.api.pagination.PageNumberPageSizePagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Engram API',
    'DESCRIPTION': 'Engineering memory layer for AI agents',
    'VERSION': '1.0.0',
}

LOG_FORMATTER = os.environ.get('LOG_FORMATTER', 'console')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
    },
    'formatters': {
        'json': {
            '()': structlog.stdlib.ProcessorFormatter,
            'processor': structlog.processors.JSONRenderer(),
        },
        'console': {
            '()': structlog.stdlib.ProcessorFormatter,
            'processor': structlog.dev.ConsoleRenderer(),
        },
    },
    'handlers': {
        'console_debug': {
            'class': 'logging.StreamHandler',
            'level': 'DEBUG',
            'formatter': LOG_FORMATTER,
            'filters': ['require_debug_true'],
            'stream': sys.stdout,
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': LOG_FORMATTER,
            'level': 'INFO',
            'stream': sys.stdout,
        },
    },
    'loggers': {
        'django.db.backends': {
            'level': 'DEBUG',
            'handlers': ['console_debug'],
        },
        '': {
            'level': 'DEBUG' if DEBUG else 'INFO',
            'handlers': ['console_debug'] if DEBUG else ['console'],
        },
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
        'django': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('ENGRAM_LOG_LEVEL', 'INFO'),
    },
}

configure_logger(log_level='DEBUG' if DEBUG else 'INFO', env_profile=ENVIRONMENT)

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


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


SECRET_KEY = os.environ.get('ENGRAM_SECRET_KEY', 'engram-development-secret')
DEBUG = to_bool(os.environ.get('ENGRAM_DEBUG', 'false'))
ALLOWED_HOSTS = csv(os.environ.get('ENGRAM_ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0'), default=('localhost',))
ROOT_URLCONF = 'settings.urls'
WSGI_APPLICATION = 'settings.wsgi.application'
ASGI_APPLICATION = 'settings.asgi.application'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'django_celery_outbox',
    'engram.core',
    'engram.access',
    'engram.hooks',
    'engram.imports',
    'engram.memory',
    'engram.context',
    'engram.health',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
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

ENGRAM_REDIS_URL = os.environ.get('ENGRAM_REDIS_URL', 'redis://redis:6379/0')
CELERY_BROKER_URL = os.environ.get('ENGRAM_CELERY_BROKER_URL', ENGRAM_REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get('ENGRAM_CELERY_RESULT_BACKEND', ENGRAM_REDIS_URL)
CELERY_TASK_IGNORE_RESULT = True
CELERY_OUTBOX_APP = 'engram.celery_app.app'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('ENGRAM_LOG_LEVEL', 'INFO'),
    },
}

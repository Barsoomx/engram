import os

from .settings import *  # noqa: F401,F403

SECRET_KEY = 'engram-test-secret'
DEBUG = False
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]
DATABASES = {
    'default': {
        **database_config(  # noqa: F405
            os.environ.get('ENGRAM_DATABASE_URL', 'postgresql://engram:engram@localhost:5432/engram'),
        ),
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': False,
    },
}
if DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3':
    SILENCED_SYSTEM_CHECKS = [
        'celery_outbox.E001',
        'celery_outbox.E006',
    ]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'null': {
            'class': 'logging.NullHandler',
        },
    },
    'root': {
        'handlers': ['null'],
        'level': 'CRITICAL',
    },
}

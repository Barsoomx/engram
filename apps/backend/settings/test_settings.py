import os

from .settings import *  # noqa: F401,F403

SECRET_KEY = 'engram-test-secret'
DEBUG = False
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]
DATABASES = {
    'default': {
        **database_config(os.environ.get('ENGRAM_DATABASE_URL', 'sqlite:///:memory:')),  # noqa: F405
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': False,
    },
}
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

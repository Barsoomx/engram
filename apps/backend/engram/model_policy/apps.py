from __future__ import annotations

from django.apps import AppConfig


class ModelPolicyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engram.model_policy'

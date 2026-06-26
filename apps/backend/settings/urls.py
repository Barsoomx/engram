from django.urls import include, path

from engram.context.views import TaskContextView
from engram.core.observability.views import metrics

urlpatterns = [
    path('-/', include('engram.health.urls')),
    path('-/metrics', metrics, name='metrics'),
    path('v1/auth/', include('engram.access.auth_urls')),
    path('v1/context', TaskContextView.as_view(), name='context-task'),
    path('v1/context/', include('engram.context.urls')),
    path('v1/hooks/', include('engram.hooks.urls')),
    path('v1/inspection/', include('engram.inspection.urls')),
    path('v1/memories/', include('engram.memory.urls')),
    path('v1/model-policy/', include('engram.model_policy.urls')),
    path('v1/observations/', include('engram.observations.urls')),
    path('v1/search/', include('engram.search.urls')),
]

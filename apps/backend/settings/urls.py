from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from engram.context.views import ContextView
from engram.core.observability.views import metrics

urlpatterns = [
    path('-/', include('engram.health.urls')),
    path('-/metrics', metrics, name='metrics'),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    path('v1/auth/', include('engram.access.auth_urls')),
    path('v1/admin/', include('engram.console.urls')),
    path('v1/context', ContextView.as_view(), name='context-task'),
    path('v1/context/', include('engram.context.urls')),
    path('v1/hooks/', include('engram.hooks.urls')),
    path('v1/imports/', include('engram.imports.urls')),
    path('v1/inspection/', include('engram.inspection.urls')),
    path('v1/memories/', include('engram.memory.urls')),
    path('v1/model-policy/', include('engram.model_policy.urls')),
    path('v1/observations/', include('engram.observations.urls')),
    path('v1/search/', include('engram.search.urls')),
]

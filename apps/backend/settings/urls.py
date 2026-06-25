from django.urls import include, path

from engram.context.views import TaskContextView

urlpatterns = [
    path('-/', include('engram.health.urls')),
    path('v1/context', TaskContextView.as_view(), name='context-task'),
    path('v1/context/', include('engram.context.urls')),
    path('v1/hooks/', include('engram.hooks.urls')),
    path('v1/memories/', include('engram.memory.urls')),
]

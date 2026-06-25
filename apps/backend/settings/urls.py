from django.urls import include, path

urlpatterns = [
    path('-/', include('engram.health.urls')),
    path('v1/hooks/', include('engram.hooks.urls')),
]

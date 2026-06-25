from django.urls import include, path

urlpatterns = [
    path('-/', include('engram.health.urls')),
]

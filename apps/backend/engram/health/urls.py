from django.urls import path

from engram.health.views import healthz, readyz, startup

urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('readyz/', readyz, name='readyz'),
    path('startup/', startup, name='startup'),
]

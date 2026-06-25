from django.urls import path

from engram.context.views import SessionStartContextView

urlpatterns = [
    path('session-start', SessionStartContextView.as_view(), name='context-session-start'),
]

from django.urls import path

from engram.context.views import SessionStartContextView, UserPromptSubmitContextView

urlpatterns = [
    path('session-start', SessionStartContextView.as_view(), name='context-session-start'),
    path('user-prompt-submit', UserPromptSubmitContextView.as_view(), name='context-user-prompt-submit'),
]

from django.urls import path

from engram.hooks.views import (
    DecisionHookView,
    ErrorHookView,
    HookDryRunView,
    PostToolUseView,
    PreToolUseView,
    SessionEndView,
    SessionStartHookView,
    UserPromptSubmitView,
)

urlpatterns = [
    path('dry-run', HookDryRunView.as_view(), name='hook-dry-run'),
    path('pre-tool-use', PreToolUseView.as_view(), name='hook-pre-tool-use'),
    path('post-tool-use', PostToolUseView.as_view(), name='hook-post-tool-use'),
    path('session-start', SessionStartHookView.as_view(), name='hook-session-start'),
    path('error', ErrorHookView.as_view(), name='hook-error'),
    path('decision', DecisionHookView.as_view(), name='hook-decision'),
    path('session-end', SessionEndView.as_view(), name='hook-session-end'),
    path('user-prompt-submit', UserPromptSubmitView.as_view(), name='hook-user-prompt-submit'),
]

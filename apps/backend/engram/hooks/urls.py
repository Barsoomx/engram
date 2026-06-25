from django.urls import path

from engram.hooks.views import HookDryRunView, PostToolUseView, SessionEndView

urlpatterns = [
    path('dry-run', HookDryRunView.as_view(), name='hook-dry-run'),
    path('post-tool-use', PostToolUseView.as_view(), name='hook-post-tool-use'),
    path('session-end', SessionEndView.as_view(), name='hook-session-end'),
]

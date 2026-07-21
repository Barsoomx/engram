from django.urls import path

from engram.memory.propose_view import MemoryProposeView
from engram.memory.views import MemoryDiffView, MemoryFeedbackView, MemoryLinksView, MemoryVersionView

urlpatterns = [
    path('propose', MemoryProposeView.as_view(), name='memory-propose'),
    path('<uuid:memory_id>/diff', MemoryDiffView.as_view(), name='memory-diff'),
    path('<uuid:memory_id>/feedback', MemoryFeedbackView.as_view(), name='memory-feedback'),
    path('<uuid:memory_id>/version', MemoryVersionView.as_view(), name='memory-version'),
    path('<uuid:memory_id>/links', MemoryLinksView.as_view(), name='memory-links'),
]

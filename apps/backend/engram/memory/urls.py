from django.urls import path

from engram.memory.views import MemoryFeedbackView, MemoryLinksView, MemoryVersionView

urlpatterns = [
    path('<uuid:memory_id>/feedback', MemoryFeedbackView.as_view(), name='memory-feedback'),
    path('<uuid:memory_id>/version', MemoryVersionView.as_view(), name='memory-version'),
    path('<uuid:memory_id>/links', MemoryLinksView.as_view(), name='memory-links'),
]

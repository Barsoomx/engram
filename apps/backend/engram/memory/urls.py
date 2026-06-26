from django.urls import path

from engram.memory.views import MemoryFeedbackView, MemoryVersionView

urlpatterns = [
    path('<uuid:memory_id>/feedback', MemoryFeedbackView.as_view(), name='memory-feedback'),
    path('<uuid:memory_id>/version', MemoryVersionView.as_view(), name='memory-version'),
]

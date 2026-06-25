from django.urls import path

from engram.memory.views import MemoryFeedbackView

urlpatterns = [
    path('<uuid:memory_id>/feedback', MemoryFeedbackView.as_view(), name='memory-feedback'),
]

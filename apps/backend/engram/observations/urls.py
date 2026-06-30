from django.urls import path

from engram.observations.views import ObservationDetailView, ObservationListView

urlpatterns = [
    path('<uuid:observation_id>', ObservationDetailView.as_view(), name='observations-detail'),
    path('', ObservationListView.as_view(), name='observations-list'),
]

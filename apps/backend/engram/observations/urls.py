from django.urls import path

from engram.observations.views import ObservationListView

urlpatterns = [
    path('', ObservationListView.as_view(), name='observations-list'),
]

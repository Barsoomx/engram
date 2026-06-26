from django.urls import path

from engram.search.views import SearchView

urlpatterns = [
    path('', SearchView.as_view(), name='memory-search'),
]

from rest_framework.routers import DefaultRouter

from engram.console.views.organizations import OrganizationViewSet
from engram.console.views.teams import TeamViewSet

router = DefaultRouter()

router.register('organizations', OrganizationViewSet, basename='admin-organization')
router.register('teams', TeamViewSet, basename='admin-team')

urlpatterns = router.urls

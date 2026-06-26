from rest_framework.routers import DefaultRouter

from engram.console.views.members import MemberViewSet
from engram.console.views.organizations import OrganizationViewSet
from engram.console.views.projects import ProjectViewSet
from engram.console.views.teams import TeamViewSet

router = DefaultRouter()

router.register('organizations', OrganizationViewSet, basename='admin-organization')
router.register('teams', TeamViewSet, basename='admin-team')
router.register('projects', ProjectViewSet, basename='admin-project')
router.register('members', MemberViewSet, basename='admin-member')

urlpatterns = router.urls

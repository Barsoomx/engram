from rest_framework.routers import DefaultRouter

from engram.console.views.api_keys import ApiKeyViewSet
from engram.console.views.members import MemberViewSet
from engram.console.views.memory_review import MemoryReviewViewSet
from engram.console.views.organizations import OrganizationViewSet
from engram.console.views.projects import ProjectViewSet
from engram.console.views.roles import RoleViewSet
from engram.console.views.teams import TeamViewSet

router = DefaultRouter()

router.register('organizations', OrganizationViewSet, basename='admin-organization')
router.register('teams', TeamViewSet, basename='admin-team')
router.register('projects', ProjectViewSet, basename='admin-project')
router.register('members', MemberViewSet, basename='admin-member')
router.register('roles', RoleViewSet, basename='admin-role')
router.register('api-keys', ApiKeyViewSet, basename='admin-api-key')
router.register('memory-review', MemoryReviewViewSet, basename='admin-memory-review')

urlpatterns = router.urls

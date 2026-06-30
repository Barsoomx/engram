from django.urls import path
from rest_framework.routers import DefaultRouter

from engram.console.views.api_keys import ApiKeyViewSet
from engram.console.views.members import MemberViewSet
from engram.console.views.memory_review import MemoryReviewViewSet
from engram.console.views.metrics import (
    MetricsActivityView,
    MetricsMemoryIngestView,
    MetricsOverviewView,
    MetricsSessionsView,
)
from engram.console.views.ops import OpsOverviewView
from engram.console.views.organizations import OrganizationViewSet
from engram.console.views.projects import ProjectViewSet
from engram.console.views.roles import RoleViewSet
from engram.console.views.search_debug import SearchDebugView
from engram.console.views.settings import (
    EmbeddingSettingsView,
    PurgeOrganizationMemoryView,
    RetrievalSettingsView,
)
from engram.console.views.teams import TeamViewSet
from engram.console.views.workflow_runs import WorkflowRunViewSet

router = DefaultRouter()

router.register('organizations', OrganizationViewSet, basename='admin-organization')
router.register('teams', TeamViewSet, basename='admin-team')
router.register('projects', ProjectViewSet, basename='admin-project')
router.register('members', MemberViewSet, basename='admin-member')
router.register('roles', RoleViewSet, basename='admin-role')
router.register('api-keys', ApiKeyViewSet, basename='admin-api-key')
router.register('workflow-runs', WorkflowRunViewSet, basename='admin-workflow-run')
router.register('memory-review', MemoryReviewViewSet, basename='admin-memory-review')

urlpatterns = router.urls + [
    path('search-debug/', SearchDebugView.as_view(), name='admin-search-debug'),
    path('ops/overview', OpsOverviewView.as_view(), name='admin-ops-overview'),
    path('settings/retrieval', RetrievalSettingsView.as_view(), name='admin-settings-retrieval'),
    path('settings/embedding', EmbeddingSettingsView.as_view(), name='admin-settings-embedding'),
    path('settings/purge', PurgeOrganizationMemoryView.as_view(), name='admin-settings-purge'),
    path('metrics/overview', MetricsOverviewView.as_view(), name='admin-metrics-overview'),
    path('metrics/memory-ingest', MetricsMemoryIngestView.as_view(), name='admin-metrics-memory-ingest'),
    path('metrics/sessions', MetricsSessionsView.as_view(), name='admin-metrics-sessions'),
    path('metrics/activity', MetricsActivityView.as_view(), name='admin-metrics-activity'),
]

from django.urls import path
from rest_framework.routers import DefaultRouter

from engram.console.views.api_keys import ApiKeyViewSet
from engram.console.views.audit_log import AuditEventViewSet
from engram.console.views.digests import DigestReviewView, WeeklyDigestView
from engram.console.views.import_cancel import AdminImportCancelView
from engram.console.views.imports import ImportJobViewSet
from engram.console.views.members import MemberViewSet
from engram.console.views.memory_export import MemoryExportView
from engram.console.views.memory_review import MemoryReviewViewSet
from engram.console.views.metrics import (
    MetricsActivityView,
    MetricsMemoryIngestView,
    MetricsOverviewView,
    MetricsSessionsView,
)
from engram.console.views.model_policy_validation import ValidateModelPoliciesView
from engram.console.views.model_setup import ApplyPresetView, ModelPresetsView, ModelSetupStatusView
from engram.console.views.ops import OpsOverviewView
from engram.console.views.organizations import OrganizationViewSet
from engram.console.views.project_digest import ProjectDigestRunView
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
router.register('audit-events', AuditEventViewSet, basename='admin-audit-event')
router.register('imports', ImportJobViewSet, basename='admin-import')

urlpatterns = router.urls + [
    path(
        'projects/<uuid:project_id>/digest/run',
        ProjectDigestRunView.as_view(),
        name='admin-project-digest-run',
    ),
    path(
        'imports/<uuid:import_id>/cancel',
        AdminImportCancelView.as_view(),
        name='admin-import-cancel',
    ),
    path('memories/export', MemoryExportView.as_view(), name='admin-memory-export'),
    path('digests/weekly', WeeklyDigestView.as_view(), name='admin-digests-weekly'),
    path('digests/<uuid:memory_id>/review', DigestReviewView.as_view(), name='admin-digests-review'),
    path('search-debug/', SearchDebugView.as_view(), name='admin-search-debug'),
    path('ops/overview', OpsOverviewView.as_view(), name='admin-ops-overview'),
    path('settings/retrieval', RetrievalSettingsView.as_view(), name='admin-settings-retrieval'),
    path('settings/embedding', EmbeddingSettingsView.as_view(), name='admin-settings-embedding'),
    path('settings/purge', PurgeOrganizationMemoryView.as_view(), name='admin-settings-purge'),
    path('metrics/overview', MetricsOverviewView.as_view(), name='admin-metrics-overview'),
    path('metrics/memory-ingest', MetricsMemoryIngestView.as_view(), name='admin-metrics-memory-ingest'),
    path('metrics/sessions', MetricsSessionsView.as_view(), name='admin-metrics-sessions'),
    path('metrics/activity', MetricsActivityView.as_view(), name='admin-metrics-activity'),
    path('model-setup/status', ModelSetupStatusView.as_view(), name='admin-model-setup-status'),
    path('model-setup/presets', ModelPresetsView.as_view(), name='admin-model-setup-presets'),
    path('model-setup/apply', ApplyPresetView.as_view(), name='admin-model-setup-apply'),
    path('model-policies/validate', ValidateModelPoliciesView.as_view(), name='admin-model-policies-validate'),
]

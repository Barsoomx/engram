from django.urls import path

from engram.inspection.views import (
    AuditEventInspectionListView,
    ContextBundleInspectionDetailView,
    ContextBundleInspectionListView,
    MemoryInspectionDetailView,
    MemoryInspectionListView,
)

urlpatterns = [
    path('memories', MemoryInspectionListView.as_view(), name='inspection-memories'),
    path('memories/<uuid:memory_id>', MemoryInspectionDetailView.as_view(), name='inspection-memory-detail'),
    path('context-bundles', ContextBundleInspectionListView.as_view(), name='inspection-context-bundles'),
    path(
        'context-bundles/<uuid:bundle_id>',
        ContextBundleInspectionDetailView.as_view(),
        name='inspection-context-bundle-detail',
    ),
    path('audit-events', AuditEventInspectionListView.as_view(), name='inspection-audit-events'),
]

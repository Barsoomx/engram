from django.urls import path

from engram.imports.views.cancel_import import CancelImportView
from engram.imports.views.create_import import CreateImportView
from engram.imports.views.finalize_import import FinalizeImportView
from engram.imports.views.import_batch import ImportBatchView
from engram.imports.views.import_detail import ImportDetailView

urlpatterns = [
    path('claude-mem', CreateImportView.as_view(), name='claude-mem-import-create'),
    path('claude-mem/<uuid:import_id>', ImportDetailView.as_view(), name='claude-mem-import-detail'),
    path('claude-mem/<uuid:import_id>/batches', ImportBatchView.as_view(), name='claude-mem-import-batches'),
    path('claude-mem/<uuid:import_id>/finalize', FinalizeImportView.as_view(), name='claude-mem-import-finalize'),
    path('claude-mem/<uuid:import_id>/cancel', CancelImportView.as_view(), name='claude-mem-import-cancel'),
]

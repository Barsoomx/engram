from __future__ import annotations

from engram.celery_app import app
from engram.imports.batch_services import ExpireStaleImportJobs


@app.task(name='engram.imports.expire_stale_import_jobs')
def expire_stale_import_jobs() -> dict[str, int]:
    return ExpireStaleImportJobs().execute()

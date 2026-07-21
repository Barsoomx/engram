from django.apps import AppConfig


class MemoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engram.memory'

    def ready(self) -> None:
        from engram.memory import candidate_work_reconciler
        from engram.memory.candidate_decision_work import get_candidate_decision_work_builder

        candidate_work_reconciler.set_candidate_decision_work_builder(get_candidate_decision_work_builder())

        return

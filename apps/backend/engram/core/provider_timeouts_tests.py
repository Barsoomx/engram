from __future__ import annotations

from engram.core import provider_timeouts as pt

_TUNED_ENV = {'ENGRAM_FLEX_PROCESSING_CEILING': '900', 'ENGRAM_TIMEOUT_LADDER_MARGIN': '30'}


def test_chat_ceiling_is_the_longest_socket_a_chat_completion_can_block_on() -> None:
    assert pt.chat_call_ceiling({}) == 600
    assert pt.chat_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}) == 900
    assert pt.chat_call_ceiling({'ENGRAM_FLEX_PROCESSING_CEILING': '700'}) == 700
    assert pt.chat_call_ceiling({'ENGRAM_FLEX_HTTP_TIMEOUT': '800'}) == 800
    assert pt.chat_call_ceiling({'ENGRAM_FLEX_PROCESSING_CEILING': '300', 'ENGRAM_PROVIDER_HTTP_TIMEOUT': '400'}) == 400


def test_embedding_ceiling_is_its_own_knob() -> None:
    assert pt.embedding_call_ceiling({}) == 30
    assert pt.embedding_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}) == 30
    assert pt.embedding_call_ceiling({'ENGRAM_EMBEDDING_HTTP_TIMEOUT': '45'}) == 45


def test_blank_values_fall_back_to_the_derived_default() -> None:
    assert pt.chat_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': ''}) == 600
    assert pt.ladder_margin({'ENGRAM_TIMEOUT_LADDER_MARGIN': '  '}) == 60


def test_every_work_type_derives_a_strictly_increasing_ladder() -> None:
    for work_type in pt.WORK_TIMEOUT_SPECS:
        timeouts = pt.resolve_work_timeouts(work_type, {})

        assert timeouts.soft_time_limit < timeouts.time_limit < timeouts.lease_seconds


def test_default_ladder_sizes_each_work_type_for_its_own_provider_calls() -> None:
    assert pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, {}) == pt.WorkTimeouts(660, 720, 780)
    assert pt.resolve_work_timeouts(pt.SESSION_DISTILLATION, {}) == pt.WorkTimeouts(1260, 1320, 1380)
    assert pt.resolve_work_timeouts(pt.CANDIDATE_DECISION, {}) == pt.WorkTimeouts(690, 750, 810)
    assert pt.resolve_work_timeouts(pt.DAILY_DIGEST, {}) == pt.WorkTimeouts(660, 720, 780)
    assert pt.resolve_work_timeouts(pt.MEMORY_EMBEDDING, {}) == pt.WorkTimeouts(90, 150, 210)


def test_ladder_follows_a_raised_socket_timeout_without_extra_configuration() -> None:
    timeouts = pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'})

    assert timeouts == pt.WorkTimeouts(960, 1020, 1080)


def test_ladder_is_env_tunable_end_to_end() -> None:
    assert pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, _TUNED_ENV) == pt.WorkTimeouts(930, 960, 990)


def test_an_explicit_soft_limit_still_derives_the_steps_above_it() -> None:
    env = {'ENGRAM_OBSERVATION_SOFT_TIME_LIMIT': '700'}

    assert pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, env) == pt.WorkTimeouts(700, 760, 820)


def test_every_step_of_the_ladder_is_individually_overridable() -> None:
    env = {
        'ENGRAM_EMBEDDING_SOFT_TIME_LIMIT': '180',
        'ENGRAM_EMBEDDING_TIME_LIMIT': '210',
        'ENGRAM_EMBEDDING_LEASE_SECONDS': '300',
    }

    assert pt.resolve_work_timeouts(pt.MEMORY_EMBEDDING, env) == pt.WorkTimeouts(180, 210, 300)


def test_a_partial_override_cannot_invert_the_ladder_of_auto_recovered_work() -> None:
    for work_type, spec in pt.WORK_TIMEOUT_SPECS.items():
        timeouts = pt.resolve_work_timeouts(work_type, {spec.hard_env: '900'})

        assert timeouts.time_limit == 900, f'{work_type} ignored its hard limit override'
        assert timeouts.time_limit < timeouts.lease_seconds, f'{work_type} lease no longer outlasts its hard limit'


def test_capacity_reports_how_many_queued_chat_calls_fit_in_a_soft_limit() -> None:
    for chat_calls in (1, 2, 3, 8):
        soft_limit = chat_calls * pt.chat_call_ceiling({}) + pt.ladder_margin({})

        assert pt.chat_call_capacity(soft_limit, {}) == chat_calls


def test_capacity_is_zero_for_limits_that_cannot_outlast_a_queued_chat_call() -> None:
    assert pt.chat_call_capacity(600, {}) == 0
    assert pt.chat_call_capacity(90, {}) == 0
    assert pt.chat_call_capacity(0, {}) == 0
    assert pt.chat_call_capacity(-120, {}) == 0


def test_every_work_type_hosts_the_provider_calls_its_spec_declares() -> None:
    for work_type, spec in pt.WORK_TIMEOUT_SPECS.items():
        soft_limit = pt.resolve_work_timeouts(work_type, {}).soft_time_limit

        assert pt.chat_call_capacity(soft_limit, {}) >= spec.chat_calls


def test_every_soft_limit_outlasts_one_worst_case_chat_call_under_any_tuning() -> None:
    for env in ({}, _TUNED_ENV, {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}, {'ENGRAM_FLEX_HTTP_TIMEOUT': '750'}):
        for work_type, spec in pt.WORK_TIMEOUT_SPECS.items():
            if not spec.chat_calls:
                continue
            soft_limit = pt.resolve_work_timeouts(work_type, env).soft_time_limit

            assert pt.chat_call_ceiling(env) < soft_limit, f'{work_type} cannot host one chat call under {env}'


def test_global_task_timeouts_host_one_queued_chat_call() -> None:
    soft, hard = pt.global_task_timeouts({})

    assert (soft, hard) == (660, 720)
    assert pt.chat_call_capacity(soft, {}) >= 1
    assert pt.global_task_timeouts({'ENGRAM_TASK_SOFT_TIME_LIMIT': '900'}) == (900, 960)


def test_worker_stop_grace_covers_the_longest_hard_limit() -> None:
    stretched = {'ENGRAM_DISTILL_TIME_LIMIT': '2000'}

    assert pt.required_stop_grace_seconds({}, worker_soft_shutdown_timeout=60) == 1380
    assert pt.required_stop_grace_seconds(stretched, worker_soft_shutdown_timeout=60) == 2060

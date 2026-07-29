from __future__ import annotations

from engram.core import provider_timeouts as pt

_FLEX_ENV = {pt.FLEX_TIMEOUT_SIZING_ENV: '1'}

_PRE_FLEX_CONTRACT = {
    pt.OBSERVATION_PROCESSING: pt.WorkTimeouts(60, 90, 120),
    pt.SESSION_DISTILLATION: pt.WorkTimeouts(600, 660, 720),
    pt.DAILY_DIGEST: pt.WorkTimeouts(180, 210, 240),
    pt.WEEKLY_DIGEST: pt.WorkTimeouts(180, 210, 240),
    pt.CANDIDATE_DECISION: pt.WorkTimeouts(240, 270, 300),
    pt.MEMORY_EMBEDDING: pt.WorkTimeouts(180, 210, 300),
}


def test_a_deployment_that_never_opted_into_flex_keeps_the_pre_flex_ladder() -> None:
    resolved = {work_type: pt.resolve_work_timeouts(work_type, {}) for work_type in pt.WORK_TIMEOUT_SPECS}

    assert resolved == _PRE_FLEX_CONTRACT
    assert pt.global_task_timeouts({}) == (120, 180)


def test_flex_sizing_is_opt_in_and_the_chat_ceiling_follows_it() -> None:
    assert pt.flex_timeout_sizing_enabled({}) is False
    assert pt.chat_call_ceiling({}) == 60
    assert pt.flex_timeout_sizing_enabled(_FLEX_ENV) is True
    assert pt.chat_call_ceiling(_FLEX_ENV) == 600


def test_flex_sizing_accepts_the_usual_operator_spellings() -> None:
    for value in ('1', 'true', 'TRUE', 'yes', 'on'):
        assert pt.flex_timeout_sizing_enabled({pt.FLEX_TIMEOUT_SIZING_ENV: value}) is True

    for value in ('0', 'false', 'no', 'off', '', '  '):
        assert pt.flex_timeout_sizing_enabled({pt.FLEX_TIMEOUT_SIZING_ENV: value}) is False


def test_chat_ceiling_is_the_longest_socket_a_chat_completion_can_block_on() -> None:
    assert pt.chat_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}) == 900
    assert pt.chat_call_ceiling({'ENGRAM_FLEX_PROCESSING_CEILING': '700'}) == 700
    assert pt.chat_call_ceiling({'ENGRAM_FLEX_HTTP_TIMEOUT': '800'}) == 800
    assert pt.chat_call_ceiling({**_FLEX_ENV, 'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}) == 900
    assert pt.chat_call_ceiling({**_FLEX_ENV, 'ENGRAM_FLEX_PROCESSING_CEILING': '300'}) == 300


def test_embedding_ceiling_is_its_own_knob() -> None:
    assert pt.embedding_call_ceiling({}) == 30
    assert pt.embedding_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}) == 30
    assert pt.embedding_call_ceiling(_FLEX_ENV) == 30
    assert pt.embedding_call_ceiling({'ENGRAM_EMBEDDING_HTTP_TIMEOUT': '45'}) == 45


def test_blank_values_fall_back_to_the_derived_default() -> None:
    blank_pin = {'ENGRAM_OBSERVATION_SOFT_TIME_LIMIT': '  '}
    contract = _PRE_FLEX_CONTRACT[pt.OBSERVATION_PROCESSING]

    assert pt.chat_call_ceiling({'ENGRAM_PROVIDER_HTTP_TIMEOUT': ''}) == 60
    assert pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, blank_pin) == contract


def test_every_work_type_derives_a_strictly_increasing_ladder() -> None:
    for env in ({}, _FLEX_ENV):
        for work_type in pt.WORK_TIMEOUT_SPECS:
            timeouts = pt.resolve_work_timeouts(work_type, env)

            assert timeouts.soft_time_limit < timeouts.time_limit < timeouts.lease_seconds


def test_enabling_flex_sizing_only_stretches_work_that_chains_chat_calls() -> None:
    growth = pt.chat_call_ceiling(_FLEX_ENV) - pt.chat_call_ceiling({})

    for work_type, spec in pt.WORK_TIMEOUT_SPECS.items():
        baseline = pt.resolve_work_timeouts(work_type, {})
        flex_sized = pt.resolve_work_timeouts(work_type, _FLEX_ENV)
        expected = spec.chat_calls * growth

        assert flex_sized.soft_time_limit - baseline.soft_time_limit == expected
        assert flex_sized.time_limit - baseline.time_limit == expected
        assert flex_sized.lease_seconds - baseline.lease_seconds == expected


def test_embedding_only_work_never_moves_when_the_chat_socket_moves() -> None:
    stretched = {**_FLEX_ENV, 'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}

    assert pt.resolve_work_timeouts(pt.MEMORY_EMBEDDING, stretched) == pt.resolve_work_timeouts(pt.MEMORY_EMBEDDING, {})


def test_ladder_follows_a_raised_socket_timeout_without_extra_configuration() -> None:
    timeouts = pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'})

    assert timeouts == pt.WorkTimeouts(900, 930, 960)


def test_a_shortened_socket_never_shrinks_the_ladder_below_the_deployment_contract() -> None:
    shortened = {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '5', 'ENGRAM_EMBEDDING_HTTP_TIMEOUT': '5'}

    for work_type in pt.WORK_TIMEOUT_SPECS:
        assert pt.resolve_work_timeouts(work_type, shortened) == _PRE_FLEX_CONTRACT[work_type]


def test_an_explicit_soft_limit_still_derives_the_steps_above_it() -> None:
    env = {'ENGRAM_OBSERVATION_SOFT_TIME_LIMIT': '700'}

    assert pt.resolve_work_timeouts(pt.OBSERVATION_PROCESSING, env) == pt.WorkTimeouts(700, 730, 760)


def test_the_distill_pins_every_shipped_env_already_carries_stay_valid() -> None:
    env = {'ENGRAM_DISTILL_SOFT_TIME_LIMIT': '600', 'ENGRAM_DISTILL_TIME_LIMIT': '660'}
    timeouts = pt.resolve_work_timeouts(pt.SESSION_DISTILLATION, env)

    assert timeouts == _PRE_FLEX_CONTRACT[pt.SESSION_DISTILLATION]
    assert pt.hosted_provider_seconds(pt.SESSION_DISTILLATION, env) <= timeouts.soft_time_limit


def test_every_step_of_the_ladder_is_individually_overridable() -> None:
    env = {
        'ENGRAM_EMBEDDING_SOFT_TIME_LIMIT': '180',
        'ENGRAM_EMBEDDING_TIME_LIMIT': '210',
        'ENGRAM_EMBEDDING_LEASE_SECONDS': '400',
    }

    assert pt.resolve_work_timeouts(pt.MEMORY_EMBEDDING, env) == pt.WorkTimeouts(180, 210, 400)


def test_a_partial_override_cannot_invert_the_ladder_of_auto_recovered_work() -> None:
    for work_type, spec in pt.WORK_TIMEOUT_SPECS.items():
        timeouts = pt.resolve_work_timeouts(work_type, {spec.hard_env: '900'})

        assert timeouts.time_limit == 900, f'{work_type} ignored its hard limit override'
        assert timeouts.time_limit < timeouts.lease_seconds, f'{work_type} lease no longer outlasts its hard limit'


def test_every_soft_limit_hosts_the_wall_time_of_the_calls_it_chains() -> None:
    for env in ({}, _FLEX_ENV, {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}, {'ENGRAM_FLEX_HTTP_TIMEOUT': '750'}):
        for work_type in pt.WORK_TIMEOUT_SPECS:
            soft_time_limit = pt.resolve_work_timeouts(work_type, env).soft_time_limit

            assert pt.hosted_provider_seconds(work_type, env) <= soft_time_limit, f'{work_type} under {env}'


def test_stretching_a_socket_leaves_every_work_types_headroom_untouched() -> None:
    for env in (_FLEX_ENV, {'ENGRAM_PROVIDER_HTTP_TIMEOUT': '900'}, {'ENGRAM_EMBEDDING_HTTP_TIMEOUT': '90'}):
        for work_type in pt.WORK_TIMEOUT_SPECS:
            baseline = pt.resolve_work_timeouts(work_type, {}).soft_time_limit - pt.hosted_provider_seconds(
                work_type,
                {},
            )
            stretched = pt.resolve_work_timeouts(work_type, env).soft_time_limit - pt.hosted_provider_seconds(
                work_type,
                env,
            )

            assert stretched == baseline >= 0, f'{work_type} lost headroom under {env}'


def test_global_task_timeouts_host_one_queued_chat_call() -> None:
    for env in ({}, _FLEX_ENV):
        soft_time_limit, time_limit = pt.global_task_timeouts(env)

        assert pt.chat_call_ceiling(env) <= soft_time_limit < time_limit

    assert pt.global_task_timeouts(_FLEX_ENV) == (660, 720)
    assert pt.global_task_timeouts({'ENGRAM_TASK_SOFT_TIME_LIMIT': '900'}) == (900, 960)


def test_worker_stop_grace_covers_the_longest_hard_limit_in_both_sizing_modes() -> None:
    assert pt.required_stop_grace_seconds({}, worker_soft_shutdown_timeout=60) == 720
    assert pt.required_stop_grace_seconds(_FLEX_ENV, worker_soft_shutdown_timeout=60) == 1800
    stretched = {'ENGRAM_DISTILL_TIME_LIMIT': '2000'}

    assert pt.required_stop_grace_seconds(stretched, worker_soft_shutdown_timeout=60) == 2060

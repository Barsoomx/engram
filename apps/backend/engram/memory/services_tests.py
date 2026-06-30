from __future__ import annotations

from decimal import Decimal

from engram.memory.services import derive_observation_confidence


class _ObservationStub:
    def __init__(
        self,
        *,
        observation_type: str,
        facts: list,
        files_read: list,
        files_modified: list,
        narrative: str,
        concepts: list,
    ) -> None:
        self.observation_type = observation_type
        self.facts = facts
        self.files_read = files_read
        self.files_modified = files_modified
        self.narrative = narrative
        self.concepts = concepts


def _thin() -> _ObservationStub:
    return _ObservationStub(
        observation_type='tool_use',
        facts=[],
        files_read=[],
        files_modified=[],
        narrative='',
        concepts=[],
    )


def test_derive_observation_confidence_thin_returns_base() -> None:
    obs = _thin()

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.500')


def test_derive_observation_confidence_rich_all_bonuses_returns_point_nine_five() -> None:
    obs = _ObservationStub(
        observation_type='decision',
        facts=['use postgres'],
        files_read=['schema.sql'],
        files_modified=[],
        narrative='We decided to use postgres for reliability.',
        concepts=['database'],
    )

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.950')


def test_derive_observation_confidence_facts_only_adds_point_one() -> None:
    obs = _thin()
    obs.facts = ['a fact']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_read_only_adds_point_one() -> None:
    obs = _thin()
    obs.files_read = ['a.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_modified_only_adds_point_one() -> None:
    obs = _thin()
    obs.files_modified = ['b.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_read_and_modified_counts_as_single_bonus() -> None:
    obs = _thin()
    obs.files_read = ['a.py']
    obs.files_modified = ['b.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_narrative_whitespace_only_no_bonus() -> None:
    obs = _thin()
    obs.narrative = '   \n  '

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.500')


def test_derive_observation_confidence_narrative_nonempty_adds_point_one() -> None:
    obs = _thin()
    obs.narrative = 'We chose this approach.'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_concepts_nonempty_adds_point_zero_five() -> None:
    obs = _thin()
    obs.concepts = ['reliability']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.550')


def test_derive_observation_confidence_durable_type_decision_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'decision'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_architecture_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'architecture'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_convention_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'convention'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_gotcha_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'gotcha'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_non_durable_types_no_bonus() -> None:
    for obs_type in ('tool_use', 'session_summary', 'error', 'unknown'):
        obs = _thin()
        obs.observation_type = obs_type

        result = derive_observation_confidence(obs)  # type: ignore[arg-type]

        assert result == Decimal('0.500'), f'expected 0.500 for type {obs_type!r}'


def test_derive_observation_confidence_result_quantized_to_three_decimals() -> None:
    obs = _thin()

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == result.quantize(Decimal('0.001'))


def test_derive_observation_confidence_clamps_to_one() -> None:
    obs = _ObservationStub(
        observation_type='decision',
        facts=['f'],
        files_read=['a.py'],
        files_modified=[],
        narrative='text',
        concepts=['c'],
    )

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert Decimal('0') <= result <= Decimal('1')


def test_derive_observation_confidence_facts_plus_files_produces_point_seven() -> None:
    obs = _thin()
    obs.facts = ['a fact']
    obs.files_read = ['a.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.700')

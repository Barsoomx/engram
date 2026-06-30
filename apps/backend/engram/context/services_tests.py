from __future__ import annotations

from dataclasses import dataclass

from engram.context.services import RetrievalMatch, _pack_to_budget, estimate_tokens


@dataclass
class _MemoryStub:
    title: str
    body: str


@dataclass
class _DocumentStub:
    memory: _MemoryStub


def _make_match(title: str, body: str, score: int = 0) -> RetrievalMatch:
    return RetrievalMatch(
        document=_DocumentStub(memory=_MemoryStub(title=title, body=body)),  # type: ignore[arg-type]
        score=score,
        matched_terms=(),
        inclusion_reason='',
    )


# estimate_tokens


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens('') == 0


def test_estimate_tokens_one_char() -> None:
    assert estimate_tokens('a') == 1


def test_estimate_tokens_three_chars() -> None:
    assert estimate_tokens('abc') == 1


def test_estimate_tokens_four_chars() -> None:
    assert estimate_tokens('abcd') == 1


def test_estimate_tokens_five_chars() -> None:
    assert estimate_tokens('abcde') == 2


def test_estimate_tokens_formula() -> None:
    for length in range(0, 100):
        text = 'a' * length
        expected = (length + 3) // 4
        assert estimate_tokens(text) == expected, f'failed for length={length}'


# _pack_to_budget — None budget (item-count behavior)


def test_pack_to_budget_none_truncates_to_limit() -> None:
    matches = tuple(_make_match(f'title{i}', f'body{i}', score=10 - i) for i in range(5))
    kept, dropped = _pack_to_budget(matches, None, 3)
    assert kept == matches[:3]
    assert dropped == matches[3:]


def test_pack_to_budget_none_limit_gte_len_keeps_all() -> None:
    matches = tuple(_make_match(f'title{i}', f'body{i}') for i in range(3))
    kept, dropped = _pack_to_budget(matches, None, 10)
    assert kept == matches
    assert dropped == ()


def test_pack_to_budget_none_empty_matches() -> None:
    kept, dropped = _pack_to_budget((), None, 5)
    assert kept == ()
    assert dropped == ()


# _pack_to_budget — token budget set


def test_pack_to_budget_small_budget_trims_lower_ranked() -> None:
    top = _make_match('A', 'B', score=100)
    lower = _make_match('C' * 500, 'D' * 500, score=50)
    matches = (top, lower)
    # top block: "- [M1] A\n  B" = 12 chars = 3 tokens; budget=4 fits top but not both
    kept, dropped = _pack_to_budget(matches, 4, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert len(dropped) == 1
    assert dropped[0] is lower


def test_pack_to_budget_large_budget_keeps_all() -> None:
    matches = tuple(_make_match(f'T{i}', f'B{i}', score=10 - i) for i in range(3))
    kept, dropped = _pack_to_budget(matches, 100000, 5)
    assert kept == matches
    assert dropped == ()


def test_pack_to_budget_over_budget_top_match_is_kept() -> None:
    top = _make_match('T' * 400, 'B' * 400, score=100)
    matches = (top,)
    # block is ~200 tokens; budget=1 < cost but top match must still be kept
    kept, dropped = _pack_to_budget(matches, 1, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert dropped == ()


def test_pack_to_budget_over_budget_top_with_more_matches() -> None:
    top = _make_match('T' * 400, 'B' * 400, score=100)
    second = _make_match('S', 'S', score=50)
    matches = (top, second)
    # budget=1; top exceeds budget but is kept; second would put us over
    kept, dropped = _pack_to_budget(matches, 1, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert len(dropped) == 1
    assert dropped[0] is second


def test_pack_to_budget_limit_respected_with_large_budget() -> None:
    matches = tuple(_make_match(f'T{i}', f'B{i}', score=10 - i) for i in range(5))
    kept, dropped = _pack_to_budget(matches, 100000, 3)
    assert len(kept) == 3
    assert len(dropped) == 2
    assert kept == matches[:3]


def test_pack_to_budget_preserves_rank_order() -> None:
    m1 = _make_match('first', 'body1', score=100)
    m2 = _make_match('second', 'body2', score=50)
    m3 = _make_match('third', 'body3', score=10)
    matches = (m1, m2, m3)
    kept, dropped = _pack_to_budget(matches, 100000, 5)
    assert list(kept) == [m1, m2, m3]


def test_pack_to_budget_two_fit_three_dropped() -> None:
    # Each block: "- [M{i}] X\n  Y" is 12 chars = 3 tokens
    # budget=8 fits 2 matches (3+3=6 <= 8), 3rd would be 9 > 8
    matches = tuple(_make_match('X', 'Y', score=10 - i) for i in range(3))
    kept, dropped = _pack_to_budget(matches, 8, 5)
    assert len(kept) == 2
    assert len(dropped) == 1
    assert kept[0] is matches[0]
    assert kept[1] is matches[1]
    assert dropped[0] is matches[2]

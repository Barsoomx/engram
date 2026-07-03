from engram.context.term_extraction import extract_exact_terms, extract_symbols


def test_extract_symbols_backticked_identifiers() -> None:
    symbols = extract_symbols('Fix', 'The bug lives in `resolve_scope()` inside `engram.context.services`.')
    assert 'resolve_scope' in symbols
    assert 'engram.context.services' in symbols


def test_extract_symbols_camel_and_snake_case() -> None:
    symbols = extract_symbols('IndexMemoryVersion change', 'update retrieval_documents via memory_version')
    assert 'IndexMemoryVersion' in symbols
    assert 'retrieval_documents' in symbols


def test_extract_symbols_ignores_prose_and_short_noise() -> None:
    assert extract_symbols('A decision', 'We chose e.g. the simpler path and moved on.') == ()


def test_extract_exact_terms_tickets_errors_constants() -> None:
    terms = extract_exact_terms(
        'Fix ENGRAM-42',
        'raises ModelPolicyError when ENGRAM_PROVIDER_MODE is unset, see #1234',
    )
    assert 'ENGRAM-42' in terms
    assert 'ModelPolicyError' in terms
    assert 'ENGRAM_PROVIDER_MODE' in terms
    assert '#1234' in terms


def test_extract_exact_terms_backticked_command() -> None:
    assert 'git rebase --abort' in extract_exact_terms('t', 'run `git rebase --abort` to recover')


def test_caps_and_dedupe() -> None:
    body = ' '.join(f'symbol_name_{i}()' for i in range(50)) + ' symbol_name_1() SYMBOL_NAME_1()'
    symbols = extract_symbols('t', body)
    assert len(symbols) == 32
    assert len({s.casefold() for s in symbols}) == 32

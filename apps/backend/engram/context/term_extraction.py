import re

_MAX_TERMS = 32
_MAX_TERM_LENGTH = 120
_MIN_SYMBOL_LENGTH = 3
_MIN_EXACT_TERM_LENGTH = 4
_MIN_DOTTED_PATH_LENGTH = 6

_BACKTICK_RE = re.compile(r'`([^`\n]{2,120})`')
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*(?:\(\))?$')
_DOTTED_PATH_RE = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b')
_CALL_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\(\)')
_CAMEL_CASE_RE = re.compile(r'\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b')
_SNAKE_CASE_RE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
_TICKET_RE = re.compile(r'\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b|(?<!\w)#\d{2,6}\b')
_ERROR_CLASS_RE = re.compile(r'\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b')
_UPPER_SNAKE_RE = re.compile(r'\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b')


def _appearance_ordered(values: list[str], *, minimum_length: int) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip()
        if len(cleaned) < minimum_length or len(cleaned) > _MAX_TERM_LENGTH:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
        if len(ordered) >= _MAX_TERMS:
            break
    return tuple(ordered)


def extract_symbols(title: str, body: str) -> tuple[str, ...]:
    text = f'{title}\n{body}'
    found: list[str] = []
    for raw in _BACKTICK_RE.findall(text):
        candidate = raw.strip()
        if _IDENTIFIER_RE.match(candidate):
            found.append(candidate.removesuffix('()'))
    for match in _DOTTED_PATH_RE.finditer(text):
        value = match.group(0)
        if len(value) >= _MIN_DOTTED_PATH_LENGTH:
            found.append(value)
    found.extend(_CALL_RE.findall(text))
    found.extend(_CAMEL_CASE_RE.findall(text))
    found.extend(_SNAKE_CASE_RE.findall(text))
    return _appearance_ordered(found, minimum_length=_MIN_SYMBOL_LENGTH)


def extract_exact_terms(title: str, body: str) -> tuple[str, ...]:
    text = f'{title}\n{body}'
    found: list[str] = []
    found.extend(_TICKET_RE.findall(text))
    found.extend(_ERROR_CLASS_RE.findall(text))
    found.extend(_UPPER_SNAKE_RE.findall(text))
    for raw in _BACKTICK_RE.findall(text):
        candidate = raw.strip()
        if ' ' in candidate and not _IDENTIFIER_RE.match(candidate):
            found.append(candidate)
    return _appearance_ordered(found, minimum_length=_MIN_EXACT_TERM_LENGTH)


def normalize_lookup_value(value: object) -> str:
    return str(value).strip().casefold()


def normalize_lookup_values(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw_values: tuple[object, ...] = (values,)
    elif isinstance(values, list | tuple | set):
        raw_values = tuple(values)
    else:
        raw_values = (values,)

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        item = normalize_lookup_value(value)
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return tuple(normalized)


def unique_text_values(*groups: object) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        if isinstance(group, str):
            raw_values: tuple[object, ...] = (group,)
        elif isinstance(group, list | tuple | set):
            raw_values = tuple(group)
        else:
            raw_values = (group,)
        for raw_value in raw_values:
            item = str(raw_value).strip()
            key = item.casefold()
            if not item or key in seen:
                continue
            seen.add(key)
            values.append(item)

    return values


def derive_retrieval_terms(metadata: dict[str, object], title: str, body: str) -> tuple[list[str], list[str]]:
    symbols = unique_text_values(
        metadata.get('symbols', []),
        extract_symbols(title, body),
    )
    exact_terms = list(
        normalize_lookup_values(
            [
                *metadata.get('exact_terms', []),
                title,
                *extract_exact_terms(title, body),
            ],
        ),
    )

    return symbols, exact_terms

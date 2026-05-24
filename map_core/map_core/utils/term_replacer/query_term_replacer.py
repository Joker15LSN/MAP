from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class QueryTermReplacementResult:
    original_query: str
    query: str
    applied_replacements: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.query != self.original_query


def _normalize_replacement_map(
    replacements: Mapping[str, str] | None,
) -> dict[str, str]:
    if not replacements:
        return {}
    return {
        source: target
        for source, target in replacements.items()
        if source and source != target
    }


def _normalize_protected_terms(terms: Collection[str] | None) -> list[str]:
    if not terms:
        return []
    return sorted({term for term in terms if term}, key=lambda item: (-len(item), item))


def _build_prefix_index(tokens: Iterable[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for token in tokens:
        index.setdefault(token[0], []).append(token)
    for candidates in index.values():
        candidates.sort(key=lambda item: (-len(item), item))
    return index


def _match_longest(
    text: str,
    cursor: int,
    index: dict[str, list[str]],
) -> str | None:
    candidates = index.get(text[cursor])
    if not candidates:
        return None
    for candidate in candidates:
        if text.startswith(candidate, cursor):
            return candidate
    return None


def _split_by_protected_terms(
    text: str,
    protected_terms: Collection[str] | None,
) -> list[tuple[str, bool]]:
    normalized_terms = _normalize_protected_terms(protected_terms)
    if not text or not normalized_terms:
        return [(text, False)]

    index = _build_prefix_index(normalized_terms)
    segments: list[tuple[str, bool]] = []
    plain_buffer: list[str] = []
    cursor = 0

    while cursor < len(text):
        matched_term = _match_longest(text, cursor, index)
        if matched_term is None:
            plain_buffer.append(text[cursor])
            cursor += 1
            continue

        if plain_buffer:
            segments.append(("".join(plain_buffer), False))
            plain_buffer = []
        segments.append((matched_term, True))
        cursor += len(matched_term)

    if plain_buffer:
        segments.append(("".join(plain_buffer), False))
    return segments


def _apply_literal_replacements_once(
    text: str,
    replacements: Mapping[str, str] | None,
) -> tuple[str, list[tuple[str, str]]]:
    normalized_replacements = _normalize_replacement_map(replacements)
    if not text or not normalized_replacements:
        return text, []

    index = _build_prefix_index(normalized_replacements.keys())
    replaced_parts: list[str] = []
    applied_replacements: list[tuple[str, str]] = []
    seen_replacements: set[tuple[str, str]] = set()
    cursor = 0

    while cursor < len(text):
        matched_source = _match_longest(text, cursor, index)
        if matched_source is None:
            replaced_parts.append(text[cursor])
            cursor += 1
            continue

        replacement = (matched_source, normalized_replacements[matched_source])
        replaced_parts.append(replacement[1])
        if replacement not in seen_replacements:
            applied_replacements.append(replacement)
            seen_replacements.add(replacement)
        cursor += len(matched_source)

    return "".join(replaced_parts), applied_replacements


def replace_query_terms(
    query: str,
    replacements: Mapping[str, str] | None = None,
    *,
    enabled: bool = False,
    protected_terms: Collection[str] | None = None,
    translations: Mapping[str, str] | None = None,
    enable_translations: bool = False,
) -> QueryTermReplacementResult:
    if not enabled or not query:
        return QueryTermReplacementResult(original_query=query, query=query)

    segments = _split_by_protected_terms(query, protected_terms)
    resolved_segments: list[str] = []
    applied_replacements: list[tuple[str, str]] = []
    seen_replacements: set[tuple[str, str]] = set()

    for segment_text, is_protected in segments:
        if is_protected:
            resolved_segments.append(segment_text)
            continue

        terminology_replaced, terminology_applied = _apply_literal_replacements_once(
            segment_text,
            replacements,
        )
        if enable_translations:
            translated_text, translated_applied = _apply_literal_replacements_once(
                terminology_replaced,
                translations,
            )
            resolved_segments.append(translated_text)
        else:
            translated_applied = []
            resolved_segments.append(terminology_replaced)

        for replacement in [*terminology_applied, *translated_applied]:
            if replacement in seen_replacements:
                continue
            applied_replacements.append(replacement)
            seen_replacements.add(replacement)

    return QueryTermReplacementResult(
        original_query=query,
        query="".join(resolved_segments),
        applied_replacements=applied_replacements,
    )

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any

STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "are", "but", "not", "you", "your", "yours",
        "yourself", "yourselves", "our", "ours", "ourselves", "their", "theirs",
        "them", "they", "these", "those", "this", "that", "with", "have", "has",
        "had", "was", "were", "been", "being", "from", "into", "onto", "about",
        "against", "between", "through", "during", "before", "after", "above",
        "below", "under", "over", "again", "further", "then", "once", "here",
        "there", "when", "where", "why", "how", "all", "any", "both", "each",
        "few", "more", "most", "other", "some", "such", "only", "own", "same",
        "than", "too", "very", "can", "will", "just", "don", "dont", "should",
        "now", "who", "whom", "whose", "which", "what", "would", "could",
        "must", "shall", "may", "might", "does", "did", "doing", "done",
        "also", "another", "while", "until", "without", "within", "upon",
        "hello", "hi", "hey", "thanks", "thank", "please", "regards", "best",
        "sincerely", "dear", "sent",
        "http", "https", "com", "net", "org", "www", "href", "img", "src",
        "alt", "div", "span",
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
        "nov", "dec", "january", "february", "march", "april", "june", "july",
        "august", "september", "october", "november", "december",
        "get", "got", "let", "its", "his", "her", "him", "she",
        "one", "two", "three", "out", "off", "see", "seen", "saw",
        "use", "used", "using", "new", "old", "like", "liked", "likes",
        "make", "made", "making", "know", "known", "knowing", "take",
        "taken", "taking", "want", "wants", "wanted", "need", "needs",
        "needed", "say", "said", "says", "saying", "way", "ways",
        "yes", "yeah",
    }
)

# Allow 2+ char tokens so phrases can contain connectors like "of" mid-phrase.
# Unigram output still requires >=3 char non-stopwords (see _unigram_terms).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def strip_html(value: str) -> str:
    if not value:
        return ""
    parser = _HTMLStripper()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return value
    return parser.text()


def _raw_tokens(text: str) -> list[str]:
    """Lowercase, ordered token stream. No stopword filtering yet — phrase
    generation needs the full sequence so mid-phrase connectors survive."""
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        term = match.group(0).lower().strip("-'")
        if len(term) < 2:
            continue
        out.append(term)
    return out


def _unigram_terms(tokens: list[str]) -> set[str]:
    result: set[str] = set()
    for term in tokens:
        if len(term) < 3:
            continue
        if term in STOPWORDS:
            continue
        result.add(term)
    return result


def _ngram_terms(tokens: list[str], n: int) -> set[str]:
    """Distinct n-gram phrases for a doc.

    Rules:
      - first and last token must be non-stopwords (otherwise we collect
        phrase fragments like "in the" or "and of the")
      - first and last token must be >= 3 chars (rules out "us of" etc.)
      - mid-phrase stopwords are allowed so "city of san francisco"-style
        phrases survive
    """
    if n < 2:
        return set()
    result: set[str] = set()
    limit = len(tokens) - n + 1
    for i in range(limit):
        chunk = tokens[i : i + n]
        first, last = chunk[0], chunk[-1]
        if first in STOPWORDS or last in STOPWORDS:
            continue
        if len(first) < 3 or len(last) < 3:
            continue
        result.add(" ".join(chunk))
    return result


def _doc_terms(doc: str, n: int) -> set[str]:
    tokens = _raw_tokens(strip_html(doc))
    if n == 1:
        return _unigram_terms(tokens)
    return _ngram_terms(tokens, n)


def _document_frequencies(docs: list[str], n: int) -> tuple[int, Counter[str]]:
    df: Counter[str] = Counter()
    for doc in docs:
        for term in _doc_terms(doc, n):
            df[term] += 1
    return len(docs), df


def analyze_distinctive(
    primary_docs: list[str],
    contrast_docs: list[str],
    *,
    n: int = 1,
    min_primary_count: int = 2,
    top_n: int = 30,
) -> list[dict[str, Any]]:
    """Return terms more common in primary than contrast, ranked by rate gap.

    `score = P(term in primary) - P(term in contrast)`. Terms must appear in
    at least `min_primary_count` primary docs to be shown.
    """
    n_primary, df_primary = _document_frequencies(primary_docs, n)
    n_contrast, df_contrast = _document_frequencies(contrast_docs, n)

    results: list[dict[str, Any]] = []
    for term, primary_count in df_primary.items():
        if primary_count < min_primary_count:
            continue
        p_primary = primary_count / n_primary if n_primary else 0.0
        contrast_count = df_contrast.get(term, 0)
        p_contrast = contrast_count / n_contrast if n_contrast else 0.0
        score = p_primary - p_contrast
        if score <= 0:
            continue
        results.append(
            {
                "term": term,
                "n": n,
                "primary_count": primary_count,
                "contrast_count": contrast_count,
                "primary_rate": round(p_primary, 4),
                "contrast_rate": round(p_contrast, 4),
                "score": round(score, 4),
            }
        )

    results.sort(
        key=lambda r: (r["score"], r["primary_count"], r["term"]),
        reverse=True,
    )
    return results[:top_n]


def analyze_distinctive_all_sizes(
    primary_docs: list[str],
    contrast_docs: list[str],
    *,
    sizes: tuple[int, ...] = (1, 2, 3),
    min_primary_count: int = 2,
    top_n_per_size: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Run analyze_distinctive for each n in `sizes`. Returns a dict keyed by
    'unigrams' / 'bigrams' / 'trigrams'. Unknown sizes are keyed as f'{n}grams'."""
    label_map = {1: "unigrams", 2: "bigrams", 3: "trigrams"}
    out: dict[str, list[dict[str, Any]]] = {}
    for size in sizes:
        key = label_map.get(size, f"{size}grams")
        # Phrases are sparser; drop the min_count floor to 2 regardless but keep
        # the caller's choice for unigrams.
        min_count = min_primary_count if size == 1 else max(2, min_primary_count)
        out[key] = analyze_distinctive(
            primary_docs,
            contrast_docs,
            n=size,
            min_primary_count=min_count,
            top_n=top_n_per_size,
        )
    return out

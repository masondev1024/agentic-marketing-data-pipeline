"""Bounded Korean query phrasing and lexical support; not semantic entailment."""

import re
import unicodedata

POLICY_VERSION = "query-support.v1"
WORD_PATTERN = re.compile(r"[가-힣]+|[a-z0-9_]+")
# Longest first; applied once, at a word boundary. This is not a morphological parser.
ENDINGS = tuple(sorted({
    "에서는", "에서", "으로", "에게", "에는", "까지", "부터", "처럼", "마다",
    "인가요", "인가", "인지", "입니까", "일까요", "일까", "이나요",
    "해야", "하는", "하나", "하고", "하면", "된", "은", "는", "이", "가",
    "을", "를", "의", "에", "와", "과", "도", "로",
    "하려면", "하거나", "하나요", "해서", "해도", "할", "해", "한다", "이다",
    "되면", "된다", "하는지", "해야만", "해요", "합니다", "하지",
}, key=lambda value: (-len(value), value)))
FRAMING_WORDS = frozenset("""
    이 그 몇 어떤 무엇 무엇을 왜 어떻게 이유 원리 개념 정의 뜻 관계
    문서 참고 내용 설명 설명해 설명해주세요 주세요 줘 알려줘 알려주세요
    필요한 필요 하는 해야 할 하나요 있나 있나요 알 수 것 때 후 전 전에 시
    우리 에서 까지 값 값이 정확히 정확한 고정 고정된 첫
    두 문장 문장으로 함께 그리고 또한 문제 생기고 생기면 사용
    위해 정보 전후 하나 보장 되나요 와 과 를 은 는
    the a an of for to in is and what why how please explain
""".split())


def unsupported_characters(text: str) -> list[str]:
    """Fail closed for scripts/symbols this bounded tokenizer would silently erase."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return sorted({
        char for char in normalized
        if not (char.isascii() and (char.isprintable() or char.isspace()))
        and not ("가" <= char <= "힣" or char.isspace())
        and unicodedata.category(char)[0] != "P"
    })


def normalized_words(text: str, *, query: bool = False) -> list[str]:
    """Keep unknown words: they must not disappear just because the corpus lacks them."""
    text = unicodedata.normalize("NFKC", text).casefold()
    # An explicit, small alias shared by query and document; never keyed by filename.
    text = re.sub(r"여러\s+번", "반복 횟수", text)
    if query:
        text = re.sub(
            r"(?<![가-힣])우리\s*(?:팀|회사)(?:에서는|에서|의|는|은)?(?=$|[^가-힣])",
            " ", text,
        )
        text = re.sub(r"문서\s+기준(?:으로)?(?=$|[^가-힣])", " ", text)
        text = re.sub(r"(?<![가-힣])정책인가요(?=$|[^가-힣])", " ", text)
    words = []
    for original in WORD_PATTERN.findall(text):
        if query and original in FRAMING_WORDS:
            continue
        word = original
        for ending in ENDINGS:
            if original == "재시도":
                break
            if word.endswith(ending) and len(word) - len(ending) >= 2:
                word = word[:-len(ending)]
                break
        if query and word in FRAMING_WORDS:
            continue
        # A bounded synonym, not a rule that removes unknown content.
        word = {"한도": "제한", "겹칠": "겹치는지", "줄이나요": "줄인다"}.get(word, word)
        words.append(word)
    return words


def check_word_support(words: list[str], document_words: list[str]) -> list[dict]:
    """Require each content word to have whole-stem evidence in the winning document.

    Only exact normalized words match. Prefix overlap cannot establish support for
    a compound noun. This intentionally favors abstention for unfamiliar language.
    """
    vocabulary = set(document_words)
    checks = []
    for word in sorted(set(words)):
        matches = [word] if word in vocabulary else []
        checks.append({"word": word, "supported": bool(matches), "matches": matches})
    return checks

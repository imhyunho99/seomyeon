"""Small Korean text helpers."""

from __future__ import annotations

RIEUL = 8


def josa(word: str, with_final: str, without_final: str) -> str:
    """Attach the particle that matches the word's last syllable.

    josa("부존재", "은", "는") -> "부존재는"; ("으로", "로") treats ㄹ as no final.
    A word ending in a non-Hangul character takes ``with_final``.
    """
    if not word:
        return without_final
    last = word[-1]
    if not "가" <= last <= "힣":
        return word + with_final
    final = (ord(last) - ord("가")) % 28
    if final == 0 or (with_final == "으로" and final == RIEUL):
        return word + without_final
    return word + with_final

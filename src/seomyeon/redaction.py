"""Deterministic pseudonymization for prior briefs before they are stored.

People are found from role labels ("원고 홍길동", "피고인 김철수") and then
replaced everywhere with stable letters (A, B, ...), so the writing style
survives while identities do not. Identifiers (resident numbers, phones,
e-mails, accounts, plates) are masked by pattern.

This is a rule-based first pass, not a guarantee: names that never appear
next to a role label are missed. The lawyer must still review the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from string import ascii_uppercase

ROLES = (
    "피고인",
    "피의자",
    "피해자",
    "고소인",
    "피고소인",
    "고발인",
    "원고",
    "피고",
    "신청인",
    "피신청인",
    "청구인",
    "피청구인",
    "채권자",
    "채무자",
    "제3채무자",
    "사건본인",
    "상대방",
    "참고인",
    "증인",
    "망",
    "변호사",
    "담당변호사",
    "변호인",
    "대표이사",
    "성명",
    "소외인",
    "소외",
)
SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민류나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부황보"
)
# 역할어 뒤에 오지만 사람 이름이 아닌 말
NOT_NAMES = {
    "주장",
    "회사",
    "법인",
    "본인",
    "대리인",
    "소송대리인",
    "변호인",
    "명의",
    "소유",
    "부부",
    "사이",
    "모두",
    "각자",
    "들은",
    "들이",
    "측은",
    "측의",
    "주식회사",
    "제출",
    "진술",
    "이름",
    "부담",
    "패소",
    "승소",
    "청구",
    "답변",
    "준비서면",
    "의견",
    "신청",
    "사이에",
    "경우",
    "정도",
    "부모",
    "반소",
    "본소",
    "금액",
    "추가",
    "소속",
    "조합",
    "지정",
    "지정서",
    "일부",
    "전부",
    "각각",
    "모친",
    "부친",
    "자녀",
    "남편",
    "아내",
    "가족",
    "대표",
    "직원",
    "소송",
    "사건",
    "주소",
    "성별",
    "나이",
    "연령",
    "직업",
    "이외",
    "이후",
    "이전",
    "이상",
    "이하",
    "당시",
    "현재",
    "일체",
    "공동",
    "명의로",
    "관련",
    "상대",
    "계약",
    "관계",
    "한편",
    "역시",
    "또한",
    "고유",
    "소재지",
    "주민등록",
    "연락처",
    "전화",
    "인적사항",
    "함께",
    "전혀",
    "원만히",
    "연락",
    "직접",
    "다시",
    "이미",
    "계속",
    "서로",
    "모르는",
    "부정행위",
    "부정한",
}
PARTICLES = (
    "에게서",
    "에게",
    "께서",
    "으로",
    "이나",
    "이며",
    "이고",
    "이다",
    "이었",
    "과의",
    "와의",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "과",
    "와",
    "도",
    "만",
    "로",
    "씨",
    "측",
    "님",
    "외",
    "등",
    "였",
)
def _spaced(role: str) -> str:
    # 서식에서 흔한 '원 고', '성  명' 같은 자간 띄어쓰기를 허용한다
    return r"\s*".join(re.escape(char) for char in role)


_ROLE_ALT = "|".join(_spaced(role) for role in sorted(ROLES, key=len, reverse=True))
# 쉼표 목록이 다음 역할어('…에게, 원고 홍길동')까지 삼키지 않도록 막는다
_LIST_ITEM = rf"(?!(?:{_ROLE_ALT})(?![가-힣]))[가-힣]{{2,5}}"
ROLE_NAME_RE = re.compile(
    rf"(?<![가-힣])(?:{_ROLE_ALT})"
    r"(?:\s*[:：]\s*|\s+|(?:와|과|및)\s+)"
    rf"([가-힣]{{2,5}}(?:\s*,\s*{_LIST_ITEM})*)"
)
FILENAME_STOPWORDS = {
    "고소장",
    "이혼등",
    "이행명",
    "진정서",
    "답변서",
    "탄원서",
    "신청서",
    "의견서",
    "청구서",
    "합의서",
    "상고장",
    "항소장",
    "준비서",
    "변호인",
    "피고인",
    "피의자",
    "피해자",
    "고소인",
    "채권자",
    "채무자",
    "신청인",
    "청구인",
    "도교법",
}
IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[주민등록번호]", re.compile(r"(?<!\d)\d{6}\s*-\s*[1-8]\d{6}(?!\d)")),
    ("[전화번호]", re.compile(r"(?<!\d)0\d{1,2}[-.\s]\d{3,4}[-.\s]\d{4}(?!\d)")),
    ("[전화번호]", re.compile(r"(?<!\d)01[016789]\d{7,8}(?!\d)")),
    ("[전자우편]", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("[계좌번호]", re.compile(r"(?<![\d-])\d{2,6}-\d{2,6}-\d{2,8}(?:-\d{1,6})?(?![\d-])")),
    ("[차량번호]", re.compile(r"(?<![\d가-힣])\d{2,3}[가-힣]\s?\d{4}(?!\d)")),
)


def _letters():
    for letter in ascii_uppercase:
        yield letter
    for first in ascii_uppercase:
        for second in ascii_uppercase:
            yield first + second


def _strip_particle(word: str) -> str:
    for particle in PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def _looks_like_name(word: str) -> bool:
    return (
        2 <= len(word) <= 4
        and word[0] in SURNAMES
        and word not in NOT_NAMES
        and word not in ROLES
    )


def find_names(text: str) -> list[str]:
    names: list[str] = []
    for match in ROLE_NAME_RE.finditer(text):
        for word in re.split(r"\s*,\s*", match.group(1)):
            candidate = _strip_particle(word)
            if _looks_like_name(candidate) and candidate not in names:
                names.append(candidate)
    return names


def names_from_filename(stem: str) -> list[str]:
    """Three-letter person names in a brief's file name (흔한 파일명 관례)."""
    cleaned = re.sub(r"\([^)]*\)|외\s*\d+|弁.*", " ", stem)
    names: list[str] = []
    for token in re.split(r"[_\s,]+", cleaned):
        if (
            re.fullmatch(r"[가-힣]{3}", token)
            and token[0] in SURNAMES
            and token not in FILENAME_STOPWORDS
            and token not in names
        ):
            names.append(token)
    return names


def _name_pattern(name: str) -> re.Pattern[str]:
    if len(name) >= 3:
        # 세 글자 이상 이름은 다른 단어 안에 우연히 들어 있을 가능성이 낮아
        # 붙여 쓴 경우('홍길동상처사진')까지 바꾼다
        return re.compile(re.escape(name))
    following = "|".join(PARTICLES)
    return re.compile(
        rf"(?<![가-힣]){re.escape(name)}(?=(?:{following})|[^가-힣]|$)"
    )


@dataclass
class Redaction:
    text: str
    replacements: dict[str, str] = field(default_factory=dict)
    identifier_count: int = 0

    def apply(self, value: str) -> str:
        """Apply the same name mapping to related text such as a title."""
        value = _mask_identifiers(value)[0]
        for name, alias in sorted(
            self.replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            value = _name_pattern(name).sub(alias, value)
        return value


def _mask_identifiers(text: str) -> tuple[str, int]:
    total = 0
    for label, pattern in IDENTIFIER_PATTERNS:
        text, count = pattern.subn(label, text)
        total += count
    return text, total


def redact(text: str, extra_names: list[str] | tuple[str, ...] = ()) -> Redaction:
    text, identifier_count = _mask_identifiers(text)

    names = find_names(text)
    for name in extra_names:
        name = name.strip()
        if len(name) >= 2 and name not in names:
            names.append(name)
    aliases = _letters()
    replacements = {name: next(aliases) for name in names}
    # 긴 이름부터 바꿔야 '김철수'가 '김철'보다 먼저 처리된다
    for name in sorted(names, key=len, reverse=True):
        text = _name_pattern(name).sub(replacements[name], text)
    return Redaction(text=text, replacements=replacements, identifier_count=identifier_count)

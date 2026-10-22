import unittest

from seomyeon.redaction import redact


class RedactionTests(unittest.TestCase):
    def test_party_names_found_by_role_are_replaced_everywhere(self) -> None:
        text = (
            "원고 홍길동\n피고 김철수\n"
            "홍길동은 2024. 3. 1. 김철수에게 금원을 송금하였습니다.\n"
            "김철수의 주장은 이유 없습니다."
        )
        result = redact(text)
        self.assertNotIn("홍길동", result.text)
        self.assertNotIn("김철수", result.text)
        self.assertIn("원고 A", result.text)
        self.assertIn("A은 2024. 3. 1. B에게", result.text)
        self.assertEqual(result.replacements["홍길동"], "A")

    def test_role_followed_by_common_noun_is_not_a_name(self) -> None:
        text = "원고 주장은 이유 없고, 피고 회사는 피고 측 대리인과 협의하였습니다."
        self.assertEqual(redact(text).text, text)

    def test_common_nouns_seen_in_real_briefs_are_not_names(self) -> None:
        text = "원고 경우에 따라 피고 정도의 차이, 신청인 부모와 채권자 금액 추가"
        self.assertEqual(redact(text).replacements, {})

    def test_adverbs_and_roles_after_particle_are_not_names(self) -> None:
        text = "피고와 함께 원고와 전혀 무관하고 피고인과 원고 사이"
        self.assertEqual(redact(text).replacements, {})

    def test_non_party_prefix(self) -> None:
        self.assertNotIn("홍길동", redact("소외 홍길동은 증언하였습니다.").text)

    def test_identifiers_are_masked(self) -> None:
        text = (
            "주민등록번호 900101-1234567, 연락처 010-1234-5678, "
            "전자우편 hong@example.com, 차량 12가3456"
        )
        result = redact(text).text
        for value in ("900101-1234567", "010-1234-5678", "hong@example.com", "12가3456"):
            self.assertNotIn(value, result)
        self.assertIn("[주민등록번호]", result)
        self.assertIn("[전화번호]", result)

    def test_case_numbers_and_dates_are_kept(self) -> None:
        text = "사건 2024가단12345 손해배상(기), 2024. 3. 1. 선고"
        self.assertEqual(redact(text).text, text)

    def test_extra_names_are_replaced(self) -> None:
        result = redact("의뢰인 윤관훈의 진술", extra_names=["윤관훈"])
        self.assertNotIn("윤관훈", result.text)

    def test_same_mapping_applies_to_title(self) -> None:
        result = redact("피고인 박영수\n박영수는 부인합니다.")
        self.assertEqual(
            result.apply("991231_형사_박영수_항소이유서"), "991231_형사_A_항소이유서"
        )

    def test_names_inside_longer_words_are_not_touched(self) -> None:
        # '이은' 같은 짧은 이름이 '이은행' 같은 단어 일부를 바꾸지 않아야 한다
        result = redact("원고 이은\n이은은 이은행 앞에서 만났습니다.")
        self.assertIn("이은행", result.text)
        self.assertIn("A은", result.text)

    def test_name_joined_to_role_by_particle(self) -> None:
        result = redact("2. 피고와 안영훈 사이의 부정행위\n안영훈은 이를 부인합니다.")
        self.assertNotIn("안영훈", result.text)

    def test_comma_separated_names_after_role(self) -> None:
        result = redact("담당변호사 나길훈, 조아훈, 안경훈\n조아훈는 검토하였습니다.")
        for name in ("나길훈", "조아훈", "안경훈"):
            self.assertNotIn(name, result.text)

    def test_spaced_role_labels(self) -> None:
        result = redact("성  명\t홍길동\n원 고  김영희\n홍길동은 김영희에게 말하였습니다.")
        self.assertNotIn("홍길동", result.text)
        self.assertNotIn("김영희", result.text)

    def test_three_letter_names_are_replaced_even_when_glued(self) -> None:
        result = redact("피해자 홍길동\n갑 제1호증 홍길동상처사진")
        self.assertNotIn("홍길동", result.text)

    def test_names_from_filename(self) -> None:
        from seomyeon.redaction import names_from_filename

        self.assertEqual(
            names_from_filename("991231_형사_장물알선_윤관훈_항소이유서_나길훈弁"),
            ["윤관훈", "나길훈"],
        )
        self.assertEqual(
            names_from_filename("(v)991231_민사_부동산가압류_백승훈외1(채권자)_가압류신청서"),
            ["백승훈"],
        )
        self.assertEqual(names_from_filename("990101 임성훈 변호인의견서(경찰-쏘굿)"), ["임성훈"])


    def test_names_starting_like_common_words_are_redacted(self) -> None:
        # 리뷰 지적: '이상', '정도', '고유', '이미'로 시작하는 실명이 통과하던 문제
        text = "피고인 이상훈은 부인하고, 원고 정도현은 다투며, 피고 고유훈에게, 원고 홍길동, 이미숙은"
        result = redact(text).text
        for name in ("이상훈", "정도현", "고유훈", "홍길동", "이미숙"):
            self.assertNotIn(name, result)

    def test_colon_without_space(self) -> None:
        self.assertNotIn("홍길동", redact("원고:홍길동\n홍길동은 송금").text)

    def test_identifiers_in_title_are_masked(self) -> None:
        result = redact("본문")
        self.assertNotIn("010-1234-5678", result.apply("홍길_010-1234-5678_고소장"))

    def test_phone_without_separators(self) -> None:
        self.assertNotIn("01012345678", redact("연락처 01012345678").text)


if __name__ == "__main__":
    unittest.main()

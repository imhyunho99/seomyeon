import unittest

from seomyeon.korean import josa


class JosaTests(unittest.TestCase):
    def test_final_consonant_selects_particle(self) -> None:
        self.assertEqual(josa("차용증 부존재", "은", "는"), "차용증 부존재는")
        self.assertEqual(josa("송금 사실", "은", "는"), "송금 사실은")
        self.assertEqual(josa("원고", "이", "가"), "원고가")
        self.assertEqual(josa("피고인", "이", "가"), "피고인이")

    def test_rieul_with_euro(self) -> None:
        self.assertEqual(josa("서울", "으로", "로"), "서울로")
        self.assertEqual(josa("법원", "으로", "로"), "법원으로")

    def test_non_hangul_ending(self) -> None:
        self.assertEqual(josa("A1", "은", "는"), "A1은")
        self.assertEqual(josa("", "은", "는"), "는")

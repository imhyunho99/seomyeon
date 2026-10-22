import io
import unittest.mock
import struct
import unittest
import zipfile
import zlib

from seomyeon.ingest import (
    IngestError,
    extract_text,
    parse_body_records,
)


def _record(tag: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (size << 20)) + payload


def _para(text: str) -> bytes:
    return _record(67, text.encode("utf-16-le"))


def _deflate(raw: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(raw) + compressor.flush()


def _hwpx(*paragraphs: str) -> bytes:
    body = "".join(
        f'<hp:p id="{i}"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>'
        for i, text in enumerate(paragraphs)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            f'<hs:sec xmlns:hp="urn:hp" xmlns:hs="urn:hs">{body}</hs:sec>',
        )
    return buffer.getvalue()


class HwpRecordTests(unittest.TestCase):
    def test_paragraph_text_records_are_joined_in_order(self) -> None:
        raw = _para("1. 사실관계") + _record(66, b"\x00" * 8) + _para("원고는 송금하였습니다.")
        self.assertEqual(
            parse_body_records(_deflate(raw), compressed=True),
            ["1. 사실관계", "원고는 송금하였습니다."],
        )

    def test_extended_size_records_are_read(self) -> None:
        long_text = "가" * 3000
        self.assertEqual(
            parse_body_records(_para(long_text), compressed=False), [long_text]
        )

    def test_inline_controls_are_dropped_and_tab_kept(self) -> None:
        # 0x0009 탭(인라인 제어, 8 wchar)과 0x000B 확장 제어(표·그림, 8 wchar)
        tab = struct.pack("<H", 9) + b"\x00" * 14
        table = struct.pack("<H", 11) + b"\x00" * 14
        payload = "갑".encode("utf-16-le") + tab + "을".encode("utf-16-le") + table
        self.assertEqual(
            parse_body_records(_record(67, payload), compressed=False), ["갑\t을"]
        )

    def test_decompression_bomb_is_rejected(self) -> None:
        bomb = _deflate(b"\x00" * (40 * 1024 * 1024))
        with self.assertRaises(IngestError):
            parse_body_records(bomb, compressed=True)

    def test_fixed_width_spaces_become_spaces(self) -> None:
        payload = "원고".encode("utf-16-le") + struct.pack("<H", 31) + "홍길".encode("utf-16-le")
        self.assertEqual(parse_body_records(_record(67, payload), compressed=False), ["원고 홍길"])

    def test_truncated_record_is_rejected(self) -> None:
        raw = _para("정상 문단")[:-2]
        with self.assertRaises(IngestError):
            parse_body_records(raw, compressed=False)


class ExtractTextTests(unittest.TestCase):
    def test_hwpx_paragraphs_and_entities(self) -> None:
        data = _hwpx("1. 결론", "원고의 청구는 &lt;기각&gt;되어야 합니다.")
        self.assertEqual(
            extract_text("서면.hwpx", data),
            "1. 결론\n원고의 청구는 <기각>되어야 합니다.",
        )

    def test_hwpx_text_after_tab_and_nested_table_paragraphs(self) -> None:
        xml = (
            '<hs:sec xmlns:hp="urn:hp" xmlns:hs="urn:hs">'
            '<hp:p><hp:run><hp:t>원고<hp:tab/>홍길동</hp:t></hp:run></hp:p>'
            '<hp:p><hp:run><hp:tbl><hp:tc><hp:subList>'
            '<hp:p><hp:run><hp:t>표 안 문단</hp:t></hp:run></hp:p>'
            '</hp:subList></hp:tc></hp:tbl></hp:run></hp:p>'
            "</hs:sec>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Contents/section0.xml", xml)
        self.assertEqual(
            extract_text("a.hwpx", buffer.getvalue()), "원고\t홍길동\n\n표 안 문단"
        )

    def test_hwpx_zip_bomb_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Contents/section0.xml", "<a>" + " " * (40 * 1024 * 1024))
        with self.assertRaises(IngestError):
            extract_text("bomb.hwpx", buffer.getvalue())

    def test_many_sections_cannot_exceed_total_limit(self) -> None:
        from seomyeon import ingest

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(5):
                body = "<hp:p><hp:run><hp:t>" + "가" * 1000 + "</hp:t></hp:run></hp:p>"
                archive.writestr(f"Contents/section{index}.xml", f'<s xmlns:hp="urn:hp">{body}</s>')
        with unittest.mock.patch.object(ingest, "MAX_TEXT_CHARS", 3000):
            with self.assertRaises(IngestError):
                extract_text("a.hwpx", buffer.getvalue())

    def test_utf16_entity_declaration_is_rejected(self) -> None:
        xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE s [<!ENTITY a "XX">]><s><p><t>&a;</t></p></s>'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Contents/section0.xml", xml.encode("utf-16"))
        with self.assertRaises(IngestError):
            extract_text("a.hwpx", buffer.getvalue())

    def test_corrupt_zip_crc_is_ingest_error(self) -> None:
        data = bytearray(_hwpx("정상 문단입니다"))
        index = data.find("정상".encode())
        data[index] ^= 0xFF
        with self.assertRaises(IngestError):
            extract_text("a.hwpx", bytes(data))

    def test_plain_text_utf8(self) -> None:
        self.assertEqual(extract_text("a.txt", "준비서면\n".encode()), "준비서면")

    def test_unknown_extension_is_rejected(self) -> None:
        with self.assertRaisesRegex(IngestError, "지원하지 않는"):
            extract_text("a.pdf", b"%PDF-1.7")

    def test_non_ole_hwp_is_rejected(self) -> None:
        with self.assertRaisesRegex(IngestError, "HWP"):
            extract_text("a.hwp", b"not an ole file")

    def test_empty_result_is_rejected(self) -> None:
        with self.assertRaisesRegex(IngestError, "읽을 수 있는"):
            extract_text("a.hwpx", _hwpx(""))


if __name__ == "__main__":
    unittest.main()

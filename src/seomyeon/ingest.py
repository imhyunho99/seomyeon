"""Text extraction for uploaded briefs (.hwp, .hwpx, .txt, .md).

Parsing never executes document content. Every stage is size-bounded so a
crafted file cannot exhaust memory: raw input, each decompressed stream, each
zip member, and the final text.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from xml.etree import ElementTree
from xml.parsers import expat

import olefile

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_TOTAL_STREAM_BYTES = 64 * 1024 * 1024
MAX_SECTIONS = 256

PARA_TEXT_TAG = 67  # HWPTAG_BEGIN(16) + 51
# 8 wchar(16바이트)를 차지하는 제어 문자. 나머지 0-31은 1 wchar.
EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
INLINE_CONTROLS = {4, 5, 6, 7, 8, 9, 19, 20}

FLAG_COMPRESSED = 0x1
FLAG_PASSWORD = 0x2
FLAG_DISTRIBUTION = 0x4

SECTION_RE = re.compile(r"Contents/section(\d+)\.xml$")


class IngestError(ValueError):
    pass


class _Budget:
    """Running totals across all sections of one file."""

    def __init__(self) -> None:
        self.stream_bytes = 0
        self.chars = 0

    def add_stream(self, size: int) -> None:
        self.stream_bytes += size
        if self.stream_bytes > MAX_TOTAL_STREAM_BYTES:
            raise IngestError("문서 본문 전체가 허용된 크기를 초과합니다.")

    def add_text(self, text: str) -> None:
        self.chars += len(text)
        if self.chars > MAX_TEXT_CHARS:
            raise IngestError("추출된 본문이 허용된 길이를 초과합니다.")


def _inflate(data: bytes) -> bytes:
    decompressor = zlib.decompressobj(-15)
    try:
        output = decompressor.decompress(data, MAX_STREAM_BYTES)
    except zlib.error as exc:
        raise IngestError("HWP 본문 압축을 풀 수 없습니다.") from exc
    if decompressor.unconsumed_tail:
        raise IngestError("HWP 본문이 허용된 크기를 초과합니다.")
    return output


def _paragraph_text(payload: bytes) -> str:
    out: list[str] = []
    index, count = 0, len(payload) // 2
    while index < count:
        code = struct.unpack_from("<H", payload, index * 2)[0]
        if code >= 32:
            out.append(chr(code))
            index += 1
        elif code in EXTENDED_CONTROLS or code in INLINE_CONTROLS:
            if code == 9:
                out.append("\t")
            index += 8
        else:
            if code == 10:
                out.append("\n")
            elif code in (30, 31):  # 묶음 빈칸, 고정폭 빈칸
                out.append(" ")
            index += 1
    return "".join(out)


def parse_body_records(
    stream: bytes, *, compressed: bool, budget: _Budget | None = None
) -> list[str]:
    """Return the paragraph texts of one HWP5 BodyText/SectionN stream."""
    budget = budget or _Budget()
    raw = _inflate(stream) if compressed else stream
    if len(raw) > MAX_STREAM_BYTES:
        raise IngestError("HWP 본문이 허용된 크기를 초과합니다.")
    budget.add_stream(len(raw))
    paragraphs: list[str] = []
    position = 0
    while position < len(raw):
        if position + 4 > len(raw):
            raise IngestError("HWP 레코드가 손상되었습니다.")
        header = struct.unpack_from("<I", raw, position)[0]
        position += 4
        tag, size = header & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if position + 4 > len(raw):
                raise IngestError("HWP 레코드가 손상되었습니다.")
            size = struct.unpack_from("<I", raw, position)[0]
            position += 4
        if position + size > len(raw):
            raise IngestError("HWP 레코드가 손상되었습니다.")
        if tag == PARA_TEXT_TAG:
            text = _paragraph_text(raw[position : position + size])
            budget.add_text(text)
            paragraphs.append(text)
        position += size
    return paragraphs


def _hwp5_text(data: bytes) -> str:
    if not olefile.isOleFile(io.BytesIO(data)):
        raise IngestError("HWP 5.0 형식 파일이 아닙니다.")
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("FileHeader"):
            raise IngestError("HWP 파일 헤더가 없습니다.")
        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise IngestError("HWP 파일 헤더가 올바르지 않습니다.")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & FLAG_PASSWORD:
            raise IngestError("암호가 걸린 HWP 파일은 읽을 수 없습니다.")
        if flags & FLAG_DISTRIBUTION:
            raise IngestError("배포용 HWP 문서는 읽을 수 없습니다.")
        sections = sorted(
            (
                entry
                for entry in ole.listdir()
                if len(entry) == 2
                and entry[0] == "BodyText"
                and re.fullmatch(r"Section\d+", entry[1])
            ),
            key=lambda entry: int(entry[1][7:]),
        )
        if len(sections) > MAX_SECTIONS:
            raise IngestError("HWP 구역 수가 너무 많습니다.")
        budget = _Budget()
        paragraphs: list[str] = []
        for entry in sections:
            if ole.get_size(entry) > MAX_STREAM_BYTES:
                raise IngestError("HWP 본문이 허용된 크기를 초과합니다.")
            paragraphs.extend(
                parse_body_records(
                    ole.openstream(entry).read(),
                    compressed=bool(flags & FLAG_COMPRESSED),
                    budget=budget,
                )
            )
    return "\n".join(paragraphs)


def _reject_dtd(xml: bytes) -> None:
    """Refuse DOCTYPE/ENTITY in any encoding (a byte search misses UTF-16)."""

    def refuse(*_args) -> None:
        raise IngestError("HWPX 본문에 허용되지 않는 선언이 있습니다.")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(xml, True)
    except expat.ExpatError as exc:
        raise IngestError("HWPX 본문을 해석할 수 없습니다.") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _run_text(element: ElementTree.Element) -> str:
    """Text of one <hp:t>, keeping text that follows inline <hp:tab/> etc."""
    parts = [element.text or ""]
    for child in element:
        if _local_name(child.tag) == "tab":
            parts.append("\t")
        parts.append(child.tail or "")
    return "".join(parts)


def _collect_paragraphs(element: ElementTree.Element, out: list[str]) -> None:
    """Depth-first; a table inside a paragraph yields its own paragraphs."""
    for child in element:
        if _local_name(child.tag) == "p":
            own: list[str] = []
            nested: list[str] = []
            _paragraph_parts(child, own, nested)
            out.append("".join(own))
            out.extend(nested)
        else:
            _collect_paragraphs(child, out)


def _paragraph_parts(
    element: ElementTree.Element, own: list[str], nested: list[str]
) -> None:
    for child in element:
        name = _local_name(child.tag)
        if name == "t":
            own.append(_run_text(child))
        elif name == "p":
            sub_own: list[str] = []
            _paragraph_parts(child, sub_own, nested)
            nested.append("".join(sub_own))
        else:
            _paragraph_parts(child, own, nested)


def _hwpx_text(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestError("HWPX 형식 파일이 아닙니다.") from exc
    with archive:
        sections = sorted(
            (
                (int(match.group(1)), info)
                for info in archive.infolist()
                if (match := SECTION_RE.fullmatch(info.filename))
            ),
            key=lambda item: item[0],
        )
        if len(sections) > MAX_SECTIONS:
            raise IngestError("HWPX 구역 수가 너무 많습니다.")
        budget = _Budget()
        paragraphs: list[str] = []
        for _, info in sections:
            if info.file_size > MAX_STREAM_BYTES:
                raise IngestError("HWPX 본문이 허용된 크기를 초과합니다.")
            with archive.open(info) as member:
                xml = member.read(MAX_STREAM_BYTES + 1)
            if len(xml) > MAX_STREAM_BYTES:
                raise IngestError("HWPX 본문이 허용된 크기를 초과합니다.")
            budget.add_stream(len(xml))
            _reject_dtd(xml)
            try:
                root = ElementTree.fromstring(xml)
            except ElementTree.ParseError as exc:
                raise IngestError("HWPX 본문을 해석할 수 없습니다.") from exc
            section: list[str] = []
            _collect_paragraphs(root, section)
            for text in section:
                budget.add_text(text)
            paragraphs.extend(section)
    return "\n".join(paragraphs)


PARSER_ERRORS = (
    # 손상·암호화된 압축, 깨진 OLE 구조 등 파서 내부 오류
    zipfile.BadZipFile,
    zlib.error,
    struct.error,
    EOFError,
    OSError,
    RuntimeError,
    NotImplementedError,
)


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError("텍스트 파일은 UTF-8이어야 합니다.") from exc


def extract_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_INPUT_BYTES:
        raise IngestError("파일이 허용된 크기(10MB)를 초과합니다.")
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    readers = {"hwp": _hwp5_text, "hwpx": _hwpx_text, "txt": _decode_text, "md": _decode_text}
    if suffix not in readers:
        raise IngestError("지원하지 않는 파일 형식입니다. HWP, HWPX, TXT, MD만 읽습니다.")
    try:
        text = readers[suffix](data)
    except PARSER_ERRORS as exc:
        raise IngestError("문서가 손상되었거나 읽을 수 없는 형식입니다.") from exc
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if not text:
        raise IngestError("파일에서 읽을 수 있는 내용이 없습니다.")
    if len(text) > MAX_TEXT_CHARS:
        raise IngestError("추출된 본문이 허용된 길이를 초과합니다.")
    return text

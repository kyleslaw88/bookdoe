"""EPUB -> KEPUB conversion.

Kobo devices read plain EPUB natively; the .kepub.epub suffix plus koboSpan markers
are what turn on per-paragraph reading-position/highlight tracking. These tests pin
structural correctness (valid zip, mimetype first/uncompressed, spans present,
content preserved) and the "never fail the whole book over one bad chapter" contract.
"""
import io
import zipfile

from app.services import kepub

PNG_LIKE = b"not really an image but bytes are bytes"


def _epub(chapters: dict[str, str]) -> bytes:
    """chapters: {href: xhtml body content}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        items = "".join(f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>' for i, href in enumerate(chapters))
        z.writestr(
            "OEBPS/content.opf",
            f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test</dc:title></metadata>'
            f'<manifest>{items}</manifest><spine/></package>',
        )
        for href, body in chapters.items():
            z.writestr(f"OEBPS/{href}", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>')
        z.writestr("OEBPS/style.css", "body { color: black; }")
    return buf.getvalue()


def test_mimetype_is_first_uncompressed_and_correct():
    out = kepub.convert(_epub({"c1.xhtml": "<p>Hello.</p>"}))
    zf = zipfile.ZipFile(io.BytesIO(out))
    assert zf.infolist()[0].filename == "mimetype"
    assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"


def test_paragraphs_are_wrapped_in_kobospan():
    out = kepub.convert(_epub({"c1.xhtml": "<p>First paragraph.</p><p>Second paragraph.</p>"}))
    zf = zipfile.ZipFile(io.BytesIO(out))
    doc = zf.read("OEBPS/c1.xhtml").decode()
    assert doc.count('class="koboSpan"') == 2
    assert "First paragraph." in doc and "Second paragraph." in doc


def test_inline_markup_is_preserved_inside_the_span():
    out = kepub.convert(_epub({"c1.xhtml": "<p>This is <em>emphasized</em> text.</p>"}))
    doc = zipfile.ZipFile(io.BytesIO(out)).read("OEBPS/c1.xhtml").decode()
    assert "<em>emphasized</em>" in doc
    assert "koboSpan" in doc


def test_a_block_containing_nested_blocks_is_not_wrapped_itself():
    """A span cannot legally contain a div/p; the div is left alone and its
    children are wrapped individually instead."""
    out = kepub.convert(_epub({"c1.xhtml": "<div><p>Nested one.</p><p>Nested two.</p></div>"}))
    doc = zipfile.ZipFile(io.BytesIO(out)).read("OEBPS/c1.xhtml").decode()
    assert doc.count('class="koboSpan"') == 2
    assert "<div><p>" in doc or "<div>\n<p" in doc or "<div>" in doc


def test_empty_blocks_are_not_wrapped():
    out = kepub.convert(_epub({"c1.xhtml": "<p></p><p>Real content.</p>"}))
    doc = zipfile.ZipFile(io.BytesIO(out)).read("OEBPS/c1.xhtml").decode()
    assert doc.count('class="koboSpan"') == 1


def test_non_content_files_pass_through_unmodified():
    out = kepub.convert(_epub({"c1.xhtml": "<p>Text.</p>"}))
    zf = zipfile.ZipFile(io.BytesIO(out))
    assert zf.read("OEBPS/style.css") == b"body { color: black; }"


def test_a_malformed_chapter_is_copied_through_rather_than_failing_the_book():
    src = _epub({"c1.xhtml": "<p>Good chapter.</p>", "c2.xhtml": "<p>Unclosed"})
    # Corrupt c2 in place with genuinely invalid XML after building the container,
    # since the helper always emits well-formed XML.
    buf = io.BytesIO(src)
    zin = zipfile.ZipFile(buf)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as z:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "OEBPS/c2.xhtml":
                data = b"<html><body><p>Unclosed tag"
            z.writestr(info, data)
    broken = out_buf.getvalue()

    converted = kepub.convert(broken)
    zf = zipfile.ZipFile(io.BytesIO(converted))
    assert "koboSpan" in zf.read("OEBPS/c1.xhtml").decode()
    # c2 falls back to its original (unconverted) bytes rather than raising.
    assert zf.read("OEBPS/c2.xhtml") == b"<html><body><p>Unclosed tag"


def test_output_is_a_valid_zip_with_all_original_entries():
    src = _epub({"c1.xhtml": "<p>One.</p>", "c2.xhtml": "<p>Two.</p>"})
    out = kepub.convert(src)
    original_names = set(zipfile.ZipFile(io.BytesIO(src)).namelist())
    converted_names = set(zipfile.ZipFile(io.BytesIO(out)).namelist())
    assert original_names == converted_names

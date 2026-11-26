"""Untrusted XML (EPUB members, indexer responses) must go through defusedxml."""
import pytest
from defusedxml import EntitiesForbidden

from app.services import scanner

BILLION = (
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    b"<x>&b;</x>"
)


def test_modules_use_defused_parser():
    import app.integrations.indexers as idx
    import app.services.convert_ebook as ce
    import app.services.covers as cv
    import app.services.detectors_quality as dq
    import app.services.kepub as kp
    import app.services.localmeta as lm

    for mod in (idx, ce, cv, dq, kp, lm, scanner):
        assert mod.safe_fromstring.__module__.startswith("defusedxml")
        with pytest.raises(EntitiesForbidden):
            mod.safe_fromstring(BILLION)


def test_encryption_check_tolerates_entity_bomb():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/encryption.xml", BILLION)
    with zipfile.ZipFile(buf) as zf:
        assert scanner._epub_drm(zf) is None


def test_assemble_mobi7_rejects_entity_bomb(tmp_path):
    from app.services import convert_ebook

    bomb = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE package [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        '<package xmlns="http://www.idpf.org/2007/opf"><metadata>&b;</metadata>'
        "<manifest/><spine/></package>"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "content.opf").write_text(bomb)
    with pytest.raises(EntitiesForbidden):
        convert_ebook._assemble_mobi7(src, tmp_path / "out.epub", title=None, authors=None)

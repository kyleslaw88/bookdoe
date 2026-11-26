from app import db
from app.services import importer


def test_naming_preview_is_separate_from_existing_files():
    wid,cid,t=db.uid(),db.uid(),db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,first_publish_date,metadata_status,added_at,updated_at) VALUES (?,?,?,'2024','resolved',?,?)",(wid,'A Great Book','Great Book, A',t,t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",(cid,'Jane Doe','Doe, Jane',t,t))
        c.execute("INSERT INTO work_contributors VALUES (?,?, 'author',0)",(wid,cid))
    p=importer.preview_name(wid,'audiobook','m4b',2)
    assert 'Jane Doe' in p['folder']
    assert len(p['files'])==2
    assert p['files'][0].endswith('.m4b')


# ---------------------------------------------------------------------------
# Automatic import on completion
# ---------------------------------------------------------------------------
def test_automatic_import_is_on_by_default():
    """Import was manual because an early defect copied 826 files / 71GB from a wrong
    source path. That is fixed and guarded, and the operator already chose the release
    when they grabbed it, so stopping to ask again at the end is friction."""
    from app.services import importer as imp
    assert imp.automatic_import_enabled() is True


def test_automatic_import_can_be_turned_off():
    from app.services import importer as imp
    db.set_setting("import.automatic", False)
    assert imp.automatic_import_enabled() is False
    db.set_setting("import.automatic", True)
    assert imp.automatic_import_enabled() is True


def test_a_completed_download_is_imported_without_being_asked(monkeypatch):
    from app.services import downloads

    calls = []
    monkeypatch.setattr(downloads, "import_completed", lambda did, *a, **k: calls.append(did))
    assert downloads._auto_import("dl-1", "Some Release") == 1
    assert calls == ["dl-1"]


def test_a_failed_automatic_import_does_not_break_the_poll(monkeypatch):
    """It must land in the queue as failed, with the reason, exactly where a manual
    import would have left it - and the other downloads must keep being tracked."""
    from app.services import downloads

    def explode(download_id, *a, **k):
        raise ValueError("No managed root folder configured for audiobook")

    monkeypatch.setattr(downloads, "import_completed", explode)
    assert downloads._auto_import("dl-2", "Some Release") == 0

    events = db.all("SELECT event_type,message FROM history ORDER BY created_at DESC LIMIT 1")
    assert events[0]["event_type"] == "download.import_failed"
    assert "No managed root" in events[0]["message"]

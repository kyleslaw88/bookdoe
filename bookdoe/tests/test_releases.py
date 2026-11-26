from app import db
from app.services import releases


def seed():
    wid,cid,t=db.uid(),db.uid(),db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,first_publish_date,metadata_status,added_at,updated_at) VALUES (?,?,?,'2021','resolved',?,?)",(wid,'Project Hail Mary','Project Hail Mary',t,t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",(cid,'Andy Weir','Weir, Andy',t,t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)",(wid,cid))
    return wid


def test_good_audio_scores_high():
    wid=seed()
    rel={"title":"Andy Weir - Project Hail Mary (2021) Unabridged M4B Retail","protocol":"usenet","size":700*1024*1024,"guid":"g1"}
    cfg={"allowed_formats":["m4b","mp3"],"preferred_formats":["m4b","mp3"],"abridged":"reject","min_size_mb":10,"max_size_mb":5000}
    score,parts,rejected,parsed=releases.score_release(wid,'audiobook',rel,cfg)
    assert score>=100
    assert not rejected
    assert parsed["format"]=="m4b"


def test_abridged_rejected():
    wid=seed()
    rel={"title":"Andy Weir Project Hail Mary ABRIDGED MP3","protocol":"torrent","size":100*1024*1024,"seeders":20,"guid":"g2"}
    cfg={"allowed_formats":["m4b","mp3"],"preferred_formats":["m4b","mp3"],"abridged":"reject","min_size_mb":10,"max_size_mb":5000}
    score,parts,rejected,parsed=releases.score_release(wid,'audiobook',rel,cfg)
    assert any('Abridged' in x for x in rejected)
    assert score < 100

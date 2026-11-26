from fastapi.testclient import TestClient
from app.main import app


def test_health_and_dashboard():
    with TestClient(app) as c:
        assert c.get('/health').status_code==200
        r=c.get('/api/v1/dashboard')
        assert r.status_code==200
        assert r.json()['counts']['works']==0


def test_opds_empty_catalog():
    with TestClient(app) as c:
        r=c.get('/opds/v2/catalog.json')
        assert r.status_code==200
        assert r.json()['publications']==[]

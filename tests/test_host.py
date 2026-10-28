from fastapi.testclient import TestClient


def test_untrusted_host_cannot_control_auth_redirects(application, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        response = client.get("/", headers={"host": "attacker.example.com"}, follow_redirects=False)
        assert response.status_code == 400
        assert "location" not in response.headers


def test_health_probe_does_not_require_public_host_header(application, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        assert client.get("/health", headers={"host": "172.20.0.10:8000"}).status_code == 200

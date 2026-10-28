import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from app.deploy import STATE, Upferry, credentials, deploy, release_config, source_archive


@pytest.fixture
def source(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("# application")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='kit'\n")
    (tmp_path / "shopify.app.toml").write_text('client_id="dev-client"\n[webhooks]\napi_version="2026-10"\n')
    (tmp_path / "shopify.app.release.toml").write_text('client_id="release-client"\nname="Example"\napplication_url="https://example.com"\n')
    (tmp_path / "upferry.json").write_text('{"deploy":{"startCommand":"wrong"}}')
    return tmp_path


def test_archive_excludes_secrets_and_sets_role(source):
    (source / ".env").write_text("SHOPIFY_API_SECRET=do-not-upload")
    (source / ".env.release").write_text("SECRET=do-not-upload")
    (source / ".env.example").write_text("SHOPIFY_API_SECRET=")
    (source / STATE).write_text("private local state")
    (source / ".venv").mkdir()
    (source / ".venv/secret").write_text("do-not-upload")
    (source / "linked-secret").symlink_to(source / ".env")
    for role in ["web", "worker"]:
        with tarfile.open(fileobj=io.BytesIO(source_archive(source, role))) as archive:
            names = archive.getnames()
            assert ".env.example" in names
            assert "shopify.app.toml" in names
            assert ".env" not in names and ".env.release" not in names
            assert STATE not in names and "linked-secret" not in names
            assert "shopify.app.release.toml" not in names
            assert not any(name.startswith(".venv/") for name in names)
            settings = json.load(archive.extractfile("upferry.json"))["deploy"]
            assert settings["startCommand"] == f"python -m app.{role}"
            assert ("healthcheckPath" in settings) == (role == "web")


def test_release_cannot_reuse_the_development_app(source):
    (source / "shopify.app.release.toml").write_text('client_id="dev-client"')
    with pytest.raises(ValueError, match="separate Shopify app"):
        release_config(source, "release")


def test_missing_encryption_key_is_not_silently_regenerated(source, monkeypatch):
    (source / STATE).write_text("{}")
    monkeypatch.setenv("SHOPIFY_API_KEY", "release-client")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secret")
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="Restore TOKEN_ENCRYPTION_KEY"):
        credentials(source, "release", {"client_id": "release-client"})


def test_full_stack_provision_reuses_resources_on_retry(source):
    calls = []

    def handle(request):
        path = request.url.path
        body = json.loads(request.content) if request.headers.get("content-type") == "application/json" else None
        calls.append((request.method, path, body))
        if path == "/projects" and request.method == "POST":
            return httpx.Response(201, json={"id": "project"})
        if path == "/projects/project":
            return httpx.Response(200, json={"id": "project"})
        if path == "/projects/project/databases":
            return httpx.Response(201, json={"service": {"id": "db", "name": "postgres"}, "deployment": {"id": "db-deploy"}})
        if path == "/projects/project/services":
            return httpx.Response(201, json={"id": body["name"]})
        if path == "/services/web":
            return httpx.Response(200, json={"domains": [{"kind": "generated", "url": "https://web.up.upferry.com"}]})
        if path.endswith("/variables"):
            assert body["variables"]["DATABASE_URL"] == "${{postgres.DATABASE_URL}}"
            return httpx.Response(200, json={})
        if path.endswith("/up"):
            with tarfile.open(fileobj=io.BytesIO(request.content)) as archive:
                assert ".env" not in archive.getnames()
                settings = json.load(archive.extractfile("upferry.json"))["deploy"]
                assert settings["startCommand"] == ("python -m app.web" if "/web/" in path else "python -m app.worker")
            return httpx.Response(202, json={"id": "deploy"})
        if path.startswith("/deployments/"):
            return httpx.Response(200, json={"status": "success"})
        raise AssertionError((request.method, path))

    api = Upferry("https://api.upferry.com", "test-token", httpx.Client(transport=httpx.MockTransport(handle)))
    values = {"SHOPIFY_API_KEY": "release-client", "SHOPIFY_API_SECRET": "secret", "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}
    for _ in range(2):
        assert deploy(source, api, values, "release", "Example") == "https://web.up.upferry.com"
    assert len([c for c in calls if c[:2] == ("POST", "/projects")]) == 1
    assert len([c for c in calls if c[1].endswith("/databases")]) == 1
    assert len([c for c in calls if c[1].endswith("/services")]) == 2
    assert "automatically_update_urls_on_dev = false" in (source / "shopify.app.release.toml").read_text()
    assert 'client_id="dev-client"' in (source / "shopify.app.toml").read_text()


def test_upferry_refuses_cleartext_and_embedded_credentials():
    for url in ["http://api.example.com", "https://user:secret@api.example.com", "https://api.example.com?token=secret"]:
        with pytest.raises(ValueError):
            Upferry(url)

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Job, Shop, WebhookReceipt, now
from app.main import create_app
from app.shopify import ShopifyService
from app.worker import claim, process_one


def signed_webhook(settings, shop="alpha-shop", topic="app/uninstalled", **extra):
    payload = {"id": 1234, "myshopify_domain": f"{shop}.myshopify.com"} if topic == "app/uninstalled" else {"shop_id": 1234, "shop_domain": f"{shop}.myshopify.com", "customer": {"id": 10, "email": "private@example.invalid"}}
    payload.update(extra)
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = base64.b64encode(hmac.new(settings.client_secret.encode(), body, hashlib.sha256).digest()).decode()
    return body, {"content-type": "application/json", "x-shopify-shop-domain": f"{shop}.myshopify.com", "x-shopify-topic": topic, "x-shopify-hmac-sha256": signature}


def test_real_sdk_exchange_encryption_and_restart(application, auth, database, settings, service, shopify_http):
    with TestClient(application, base_url=settings.app_url) as client:
        # Shopify's initial document request carries an ID token; subsequent
        # App Bridge fetches use the Authorization header instead.
        response = client.get("/", params={"id_token": auth()["authorization"].removeprefix("Bearer ")})
        assert response.status_code == 200, response.text
        assert "Example Shop" in response.text
        assert "alpha-shop.myshopify.com" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
    with database[1]() as session:
        record = session.get(Shop, "alpha-shop")
        assert "shpat_" not in record.token_ciphertext
        assert "shprt_" not in record.token_ciphertext
        assert record.shop_id == "1234"
    restarted = create_app(settings, database[1], ShopifyService(settings, database[1], http_client=shopify_http.client))
    with TestClient(restarted, base_url=settings.app_url) as client:
        assert client.get("/api/shop", headers=auth()).json()["name"] == "Example Shop"
    assert len(shopify_http.grants) == 1


def test_wrong_audience_and_expired_tokens_cannot_access(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        for headers in [auth(aud="some-other-app"), auth(exp=now() - timedelta(minutes=5)), {"authorization": "Bearer forged"}]:
            assert client.post("/api/jobs", headers=headers, json={}).status_code != 202
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(Shop)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_jobs_are_scoped_and_completed_by_separate_worker(application, auth, database, service, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        queued = client.post("/api/jobs", headers=auth(), json={})
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        assert client.get(f"/api/jobs/{job_id}", headers=auth("beta-shop")).status_code == 404
        assert client.get(f"/api/jobs/{job_id}?shop=alpha-shop", headers=auth("beta-shop")).status_code == 404
        assert process_one(database[1], service)
        result = client.get(f"/api/jobs/{job_id}", headers=auth()).json()
        assert result["status"] == "success"
        assert result["result"]["shopName"] == "Example Shop"
        assert result["result"]["runtime"].startswith("Python ")


def test_concurrent_refresh_uses_one_persisted_pair(application, auth, database, service, settings, shopify_http):
    with TestClient(application, base_url=settings.app_url) as client:
        assert client.get("/api/shop", headers=auth()).status_code == 200
    with database[1].begin() as session:
        record = session.get(Shop, "alpha-shop")
        token = json.loads(service.cipher.decrypt(record.token_ciphertext.encode()))
        token["expires"] = (now() - timedelta(minutes=1)).isoformat()
        record.token_ciphertext = service.cipher.encrypt(json.dumps(token).encode()).decode()
    with ThreadPoolExecutor(max_workers=10) as pool:
        tokens = list(pool.map(lambda _: service.token("alpha-shop"), range(10)))
    assert len({token["refresh_token"] for token in tokens}) == 1
    assert shopify_http.grants.count("refresh_token") == 1


def test_two_workers_do_not_claim_the_same_job(application, auth, database, service, settings, shopify_http):
    with TestClient(application, base_url=settings.app_url) as client:
        assert client.post("/api/jobs", headers=auth(), json={}).status_code == 202
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process_one(database[1], service), range(2)))
    assert results.count(True) == 1
    assert shopify_http.graphql_calls == 1


def test_crashed_worker_lease_is_reclaimed_and_old_lease_changes(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        job_id = client.post("/api/jobs", headers=auth(), json={}).json()["id"]
    first = claim(database[1])
    with database[1].begin() as session:
        session.get(Job, job_id).lease_until = now() - timedelta(seconds=1)
    second = claim(database[1])
    assert second["id"] == first["id"]
    assert second["lease"] != first["lease"]
    assert second["attempts"] == 2


def test_last_expired_attempt_becomes_failed(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        job_id = client.post("/api/jobs", headers=auth(), json={}).json()["id"]
    with database[1].begin() as session:
        job = session.get(Job, job_id)
        job.status, job.attempts, job.lease = "running", 3, "dead-worker"
        job.lease_until = now() - timedelta(seconds=1)
    assert claim(database[1]) is None
    with database[1]() as session:
        assert session.get(Job, job_id).status == "failed"


def test_invalid_webhook_cannot_delete_installation(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        client.get("/api/shop", headers=auth())
        body, headers = signed_webhook(settings)
        headers["x-shopify-hmac-sha256"] = "forged"
        assert client.post("/webhooks/app/uninstalled", headers=headers, content=body).status_code == 401
    with database[1]() as session:
        assert session.get(Shop, "alpha-shop") is not None


def test_signed_body_cannot_be_retargeted_to_another_shop(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        client.get("/api/shop", headers=auth("beta-shop"))
        body, headers = signed_webhook(settings)
        headers["x-shopify-shop-domain"] = "beta-shop.myshopify.com"
        assert client.post("/webhooks/app/uninstalled", headers=headers, content=body).status_code == 400
    with database[1]() as session:
        assert session.get(Shop, "beta-shop") is not None


def test_uninstall_removes_tokens_and_jobs_and_can_repeat(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        client.get("/api/shop", headers=auth())
        client.post("/api/jobs", headers=auth(), json={})
        body, headers = signed_webhook(settings)
        assert client.post("/webhooks/app/uninstalled", headers=headers, content=body).status_code == 200
        assert client.post("/webhooks/app/uninstalled", headers=headers, content=body).status_code == 200
    with database[1]() as session:
        assert session.get(Shop, "alpha-shop") is None
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_privacy_retries_are_deduplicated_without_customer_payload(application, auth, database, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        client.get("/api/shop", headers=auth())
        body, headers = signed_webhook(settings, topic="customers/data_request")
        assert client.post("/webhooks/privacy", headers=headers, content=body).status_code == 200
        assert client.post("/webhooks/privacy", headers=headers, content=body).status_code == 200
        with database[1]() as session:
            assert session.scalar(select(func.count()).select_from(WebhookReceipt)) == 1
            assert "private@example.invalid" not in str(session.scalars(select(WebhookReceipt)).all())
        body, headers = signed_webhook(settings, topic="shop/redact")
        assert client.post("/webhooks/privacy", headers=headers, content=body).status_code == 200
    with database[1]() as session:
        assert session.get(Shop, "alpha-shop") is None
        assert session.scalar(select(func.count()).select_from(WebhookReceipt)) == 0


def test_body_limit_is_enforced_before_verification(application, settings):
    with TestClient(application, base_url=settings.app_url) as client:
        assert client.post("/webhooks/privacy", content=b"x" * (1024 * 1024 + 1)).status_code == 413

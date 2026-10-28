import json
import os
from datetime import timedelta
from threading import Lock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db import Base, now
from app.main import create_app
from app.settings import Settings
from app.shopify import ShopifyService


@pytest.fixture
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set TEST_DATABASE_URL to a disposable Postgres database; see README")
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    admin = create_engine(url)
    schema = "kit_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(test_url, pool_size=15, max_overflow=5)
    Base.metadata.create_all(engine)
    yield engine, sessionmaker(engine, expire_on_commit=False), test_url.render_as_string(hide_password=False)
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture
def settings(database):
    return Settings(database_url=database[2], client_id="a" * 32, client_secret="b" * 64, encryption_key=Fernet.generate_key().decode(), app_url="https://kit.example.com")


class ShopifyHTTP:
    def __init__(self):
        self.grants = []
        self.graphql_calls = 0
        self.fail_graphql = False
        self.lock = Lock()
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        shop = request.url.host.split(".")[0]
        if request.url.path == "/admin/oauth/access_token":
            values = parse_qs(request.content.decode()) if "application/x-www-form-urlencoded" in request.headers.get("content-type", "") else json.loads(request.content)
            grant = values.get("grant_type")
            if isinstance(grant, list):
                grant = grant[0]
            with self.lock:
                self.grants.append(grant)
                number = len(self.grants)
            return httpx.Response(200, json={"access_token": f"shpat_test_{shop}_{number}", "scope": "", "expires_in": 3600, "refresh_token": f"shprt_test_{shop}_{number}", "refresh_token_expires_in": 7776000})
        if request.url.path.endswith("/graphql.json"):
            with self.lock:
                self.graphql_calls += 1
            if self.fail_graphql:
                return httpx.Response(401, json={"errors": "Unauthorized"})
            return httpx.Response(200, json={"data": {"shop": {"id": "gid://shopify/Shop/1234", "name": "Example Shop", "myshopifyDomain": f"{shop}.myshopify.com"}}})
        raise AssertionError(f"Unexpected Shopify endpoint {request.url.path}")


@pytest.fixture
def shopify_http():
    value = ShopifyHTTP()
    yield value
    value.client.close()


@pytest.fixture
def service(settings, database, shopify_http):
    return ShopifyService(settings, database[1], http_client=shopify_http.client)


@pytest.fixture
def application(settings, database, service):
    return create_app(settings, database[1], service)


@pytest.fixture
def auth(settings):
    def headers(shop="alpha-shop", **overrides):
        claims = {"iss": f"https://{shop}.myshopify.com/admin", "dest": f"https://{shop}.myshopify.com", "aud": settings.client_id, "sub": "123", "exp": now() + timedelta(minutes=1), "nbf": now() - timedelta(seconds=1), "iat": now(), "jti": str(uuid4()), "sid": "test-session"}
        claims.update(overrides)
        return {"authorization": "Bearer " + jwt.encode(claims, settings.client_secret, algorithm="HS256"), "accept": "application/json"}
    return headers

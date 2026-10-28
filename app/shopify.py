import json
from dataclasses import asdict

from cryptography.fernet import Fernet
from shopify_app import ShopifyApp
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import Shop


class ShopifyFailure(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result.log.code)


class Reauthorize(Exception):
    pass


class ShopifyService:
    def __init__(self, settings, sessions, sdk=None, http_client=None):
        self.settings = settings
        self.sessions = sessions
        self.sdk = sdk or ShopifyApp(
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            old_client_secret=settings.old_client_secret,
        )
        self.cipher = Fernet(settings.encryption_key.encode())
        self.http_client = http_client

    def token(self, shop, verified=None):
        # Every process uses the same row lock. An existing token is read again
        # after locking; the refreshed access/refresh pair commits BEFORE GraphQL.
        with self.sessions.begin() as session:
            if verified is not None:
                session.execute(insert(Shop).values(name=shop).on_conflict_do_nothing())
            record = session.scalar(select(Shop).where(Shop.name == shop).with_for_update())
            if record is None:
                raise Reauthorize()
            token = json.loads(self.cipher.decrypt(record.token_ciphertext.encode())) if record.token_ciphertext else None
            if token:
                result = self.sdk.refresh_token_exchanged_access_token(token, http_client=self.http_client)
                if result.ok:
                    if result.access_token:
                        token = asdict(result.access_token)
                elif verified is None:
                    raise ShopifyFailure(result)
                else:
                    token = None
            if not token:
                if verified is None:
                    raise Reauthorize()
                result = self.sdk.exchange_using_token_exchange(
                    access_mode="offline",
                    id_token=verified.id_token,
                    invalid_token_response=verified.invalid_token_response,
                    http_client=self.http_client,
                )
                if not result.ok:
                    raise ShopifyFailure(result)
                token = asdict(result.access_token)
            if token["shop"] != shop or token["access_mode"] != "offline":
                raise RuntimeError("Shopify token did not match the verified installation")
            record.token_ciphertext = self.cipher.encrypt(json.dumps(token).encode()).decode()
        return token

    def shop_details(self, shop, verified=None):
        token = self.token(shop, verified)
        result = self.sdk.admin_graphql_request(
            "query LaunchKitShop { shop { id name myshopifyDomain } }",
            shop=shop,
            access_token=token["token"],
            api_version=self.settings.api_version,
            invalid_token_response=verified.invalid_token_response if verified else None,
            http_client=self.http_client,
        )
        if not result.ok:
            if result.log.code == "unauthorized":
                with self.sessions.begin() as session:
                    record = session.scalar(select(Shop).where(Shop.name == shop).with_for_update())
                    # Do not clear a token that another request refreshed meanwhile.
                    if record and record.token_ciphertext:
                        stored = json.loads(self.cipher.decrypt(record.token_ciphertext.encode()))
                        if stored["token"] == token["token"]:
                            record.token_ciphertext = None
            raise ShopifyFailure(result)
        details = result.data["shop"]
        if details["myshopifyDomain"] != f"{shop}.myshopify.com":
            raise RuntimeError("Shopify shop did not match the verified installation")
        with self.sessions.begin() as session:
            record = session.get(Shop, shop)
            if record:
                record.shop_id = details["id"].rsplit("/", 1)[-1]
        return details

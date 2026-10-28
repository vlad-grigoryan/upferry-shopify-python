import hashlib
import html
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.db import Job, Shop, WebhookReceipt, database
from app.settings import Settings
from app.shopify import Reauthorize, ShopifyFailure, ShopifyService

logger = logging.getLogger("launch-kit")
MAX_BODY = 1024 * 1024


async def sdk_request(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Request body is too large")
        body.extend(chunk)
    try:
        value = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Expected UTF-8 request body")
    return {"method": request.method, "url": str(request.url), "headers": dict(request.headers), "body": value}


def sdk_response(result):
    # Deliberately do not log result.log.req: it can contain credentials.
    logger.info("Shopify response: %s", result.log.code)
    return Response(result.response.body, status_code=result.response.status, headers=result.response.headers)


def create_app(settings=None, sessions=None, service=None):
    settings = settings or Settings.from_env()
    engine = None
    if sessions is None:
        engine, sessions = database(settings.database_url)
    service = service or ShopifyService(settings, sessions)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if engine:
            engine.dispose()

    app = FastAPI(title="Shopify Python Launch Kit", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ShopifyFailure)
    async def shopify_error(_request, error):
        return sdk_response(error.result)

    @app.exception_handler(Reauthorize)
    async def reauthorize(_request, _error):
        return JSONResponse({"error": "Open this app in Shopify admin to authorize it again"}, status_code=401)

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.url.path != "/health" and request.url.hostname != urlsplit(settings.app_url).hostname:
            return Response("Unexpected host", status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Successful App Home verification supplies its own scoped frame-ancestors.
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    async def verify(request):
        raw = await sdk_request(request)
        result = service.sdk.verify_app_home_req(raw, app_home_patch_id_token_path="/auth/patch-id-token")
        return result

    @app.get("/health")
    def health():
        with sessions() as session:
            session.execute(text("SELECT 1"))
            session.execute(select(Shop.name).limit(1))
        return {"status": "ok", "kit": "shopify-python", "version": "0.1.0"}

    @app.get("/auth/patch-id-token")
    async def patch_id_token(request: Request):
        return sdk_response(service.sdk.app_home_patch_id_token(await sdk_request(request)))

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        verified = await verify(request)
        if not verified.ok:
            return sdk_response(verified)
        details = await run_in_threadpool(service.shop_details, verified.shop, verified)
        content = Path(__file__).with_name("home.html").read_text()
        content = content.replace("{{CLIENT_ID}}", html.escape(settings.client_id, quote=True))
        content = content.replace("{{SHOP_NAME}}", html.escape(details["name"]))
        content = content.replace("{{SHOP_DOMAIN}}", html.escape(details["myshopifyDomain"]))
        return HTMLResponse(content, headers=verified.response.headers)

    @app.get("/api/shop")
    async def shop(request: Request):
        verified = await verify(request)
        if not verified.ok:
            return sdk_response(verified)
        details = await run_in_threadpool(service.shop_details, verified.shop, verified)
        return JSONResponse(details, headers=verified.response.headers)

    @app.post("/api/jobs", status_code=202)
    async def enqueue_job(request: Request):
        verified = await verify(request)
        if not verified.ok:
            return sdk_response(verified)
        # Token exchange/refresh finishes before creating a job; a verification
        # alone never gives a caller access to another shop's data.
        await run_in_threadpool(service.token, verified.shop, verified)

        def save():
            with sessions.begin() as session:
                session.scalar(select(Shop).where(Shop.name == verified.shop).with_for_update())
                active = session.scalar(select(func.count()).select_from(Job).where(Job.shop == verified.shop, Job.status.in_(["queued", "running"])))
                if active >= 10:
                    raise HTTPException(429, "Wait for the existing jobs to finish")
                job = Job(shop=verified.shop)
                session.add(job)
                session.flush()
                return job.id

        job_id = await run_in_threadpool(save)
        return JSONResponse({"id": job_id, "status": "queued"}, status_code=202, headers=verified.response.headers)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        verified = await verify(request)
        if not verified.ok:
            return sdk_response(verified)

        def read():
            with sessions() as session:
                job = session.scalar(select(Job).where(Job.id == job_id, Job.shop == verified.shop))
                if not job:
                    raise HTTPException(404, "Job not found")
                return {"id": job.id, "status": job.status, "attempts": job.attempts, "result": job.result, "error": job.error}

        return JSONResponse(await run_in_threadpool(read), headers=verified.response.headers)

    async def webhook(request, privacy=False):
        raw = await sdk_request(request)
        verified = service.sdk.verify_webhook_req(raw)
        if not verified.ok:
            return sdk_response(verified)
        topic = request.headers.get("x-shopify-topic", "")
        allowed = {"customers/data_request", "customers/redact", "shop/redact"} if privacy else {"app/uninstalled"}
        if topic not in allowed:
            raise HTTPException(400, "Unexpected webhook topic")
        try:
            payload = json.loads(raw["body"])
        except ValueError:
            raise HTTPException(400, "Invalid webhook JSON")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a webhook object")
        signed_domain = payload.get("shop_domain") if privacy else payload.get("myshopify_domain")
        signed_id = payload.get("shop_id") if privacy else payload.get("id")
        # Shopify's body HMAC does not bind arbitrary headers. Bind the verified
        # header shop to the domain AND id inside the signed JSON before deletion.
        if signed_domain != f"{verified.shop}.myshopify.com" or not signed_id:
            raise HTTPException(400, "Webhook shop does not match its signed body")
        receipt_id = hashlib.sha256((verified.shop + "\n" + topic + "\n" + raw["body"]).encode()).hexdigest()

        def apply():
            with sessions.begin() as session:
                installed = session.scalar(select(Shop).where(Shop.name == verified.shop).with_for_update())
                if installed and installed.shop_id and installed.shop_id != str(signed_id):
                    raise HTTPException(400, "Webhook shop id does not match the installation")
                if topic in {"app/uninstalled", "shop/redact"}:
                    session.execute(delete(Job).where(Job.shop == verified.shop))
                    session.execute(delete(Shop).where(Shop.name == verified.shop))
                    session.execute(delete(WebhookReceipt).where(WebhookReceipt.shop == verified.shop))
                else:
                    # The starter stores no customer records. Record only a hash,
                    # topic and shop, never the customer payload or email address.
                    session.execute(insert(WebhookReceipt).values(id=receipt_id, shop=verified.shop, topic=topic).on_conflict_do_nothing())

        await run_in_threadpool(apply)
        logger.info("Shopify webhook processed: %s", topic)
        return Response(status_code=200)

    @app.post("/webhooks/app/uninstalled")
    async def uninstall(request: Request):
        return await webhook(request)

    @app.post("/webhooks/privacy")
    async def privacy(request: Request):
        return await webhook(request, privacy=True)

    return app

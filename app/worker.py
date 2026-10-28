import logging
import signal
import sys
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from app.db import Job, database, now
from app.settings import Settings
from app.shopify import ShopifyService

logger = logging.getLogger("launch-kit.worker")
MAX_ATTEMPTS = 3
LEASE_SECONDS = 180


def claim(sessions):
    at = now()
    with sessions.begin() as session:
        job = session.scalar(select(Job).where(
            Job.attempts < MAX_ATTEMPTS,
            or_(and_(Job.status == "queued", Job.available_at <= at), and_(Job.status == "running", Job.lease_until < at)),
        ).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            # A process that died on its last attempt must not leave a permanent running job.
            session.execute(update(Job).where(Job.status == "running", Job.lease_until < at, Job.attempts >= MAX_ATTEMPTS).values(status="failed", error="Worker stopped during the last attempt", lease=None, lease_until=None))
            return None
        job.status = "running"
        job.attempts += 1
        job.lease = str(uuid4())
        job.lease_until = at + timedelta(seconds=LEASE_SECONDS)
        return {"id": job.id, "shop": job.shop, "lease": job.lease, "attempts": job.attempts}


def process_one(sessions, service):
    job = claim(sessions)
    if not job:
        return False
    try:
        # Read-only example: retries cannot duplicate a merchant-side write.
        details = service.shop_details(job["shop"])
        changes = {"status": "success", "result": {"shopName": details["name"], "runtime": f"Python {sys.version_info.major}.{sys.version_info.minor}", "processedAt": now().isoformat()}, "error": None}
    except Exception as error:
        # Never persist exception text: HTTP/SDK exceptions may contain tokens.
        logger.warning("Job attempt failed (%s)", type(error).__name__)
        failed = job["attempts"] >= MAX_ATTEMPTS
        changes = {"status": "failed" if failed else "queued", "error": "Shopify request failed. Open the app to authorize it again, then retry." if failed else None, "available_at": now() + timedelta(seconds=5 * job["attempts"])}
    with sessions.begin() as session:
        session.execute(update(Job).where(Job.id == job["id"], Job.status == "running", Job.lease == job["lease"]).values(**changes, lease=None, lease_until=None))
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine, sessions = database(settings.database_url)
    service = ShopifyService(settings, sessions)
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Python worker started")
    try:
        while not stopping:
            try:
                if not process_one(sessions, service):
                    time.sleep(1)
            except Exception as error:
                logger.warning("Worker database unavailable (%s)", type(error).__name__)
                time.sleep(3)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

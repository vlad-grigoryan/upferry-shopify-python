# Shopify Python Launch Kit

A portable FastAPI app using Shopify's official `shopifyapp` package, Postgres and a separate Python worker. Deploy the full stack on Upferry or run the same containers on another host.

The sample installs into a development store, fetches its name through the Admin GraphQL API, and lets you run a read-only background job. Installation tokens are encrypted in Postgres and survive web deployments. A Postgres row lock coordinates token refresh across processes.

This is an independent Upferry project, not an official Shopify template or a guarantee of App Store approval.

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+ and [Shopify CLI](https://shopify.dev/docs/api/shopify-cli).
- A Shopify Partner account and a development store.
- Docker for local Postgres. Docker is not needed on your laptop for Upferry deployments.

## Create your app

```sh
npx @shopify/cli app init --template https://github.com/vlad-grigoryan/upferry-shopify-python --name my-python-app
cd my-python-app
uv sync
docker compose up -d
cp .env.example .env
```

Generate `TOKEN_ENCRYPTION_KEY` and paste the output into your local `.env`:

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
uv run alembic upgrade head
```

Shopify CLI supplies the development app credentials and tunnel URL to the web process:

```sh
npx @shopify/cli app dev --config shopify.app.toml
```

For the separate local worker, obtain credentials for your development app with `npx @shopify/cli app env show --config shopify.app.toml` and add them to your private `.env`. Never commit this file. Then run in another terminal:

```sh
uv run python -m app.worker
```

Open the app in your development store and select **Run Python job**. The job fetches your shop name on the worker and shows its result in the app. No paid AI API or real customer records are needed.

## Launch on Upferry

Upferry hosting currently requires an approved GitHub account. Public self-service sign-up is not available yet. The source and local workflow are available to everyone; use another container host or your own Upferry installation if you do not have hosted access.

Use a separate Shopify app for the hosted release, so local development cannot overwrite its URLs:

```sh
npx @shopify/cli app config link --config release
uv run python -m app.deploy
```

Choose or create a new release app when linking. Shopify CLI may make it the default, so keep using the explicit development command above for local work. The deployment command refuses to use the same client ID as your development config. It reads the release credentials through Shopify CLI without printing them, signs in to Upferry, creates Postgres plus web and worker services, deploys the web schema, then starts the worker. Your encryption key is generated if missing and kept in private `.env`.

The command updates only `shopify.app.release.toml`, leaves the development config in place, and invokes Shopify CLI to review and publish the release. To review separately, use `uv run python -m app.deploy --skip-shopify-release` and then `npx @shopify/cli app deploy --config release`.

Finish by installing the release app into your development store from Shopify Dev Dashboard. Developer authentication and consent are required. Opening a generated hosting URL alone does not install the app.

Run the deploy command again for subsequent changes. `.upferry-shopify.json` records the resources in this folder, so retries use the same project. Secret files, private local state and virtual environments are excluded from source uploads. Environment variables go to the authenticated Upferry API over HTTPS.

For your own Upferry installation set `UPFERRY_API_URL`. A `UPFERRY_TOKEN` can be supplied for CI. Do not use arbitrary API endpoints with production credentials.

## What to verify

1. Install on a real development store and confirm its shop name appears.
2. Run a job and see `success`, a Python version and a timestamp.
3. Deploy the web again. Open the app without reinstalling it and run another job.
4. Send a webhook with an invalid signature and confirm it is rejected.
5. Uninstall the app and verify that the stored installation and its jobs are removed.

Health is available at `/health`. Access logs are disabled because Shopify page query strings can contain tokens. Do not enable request-body or authorization-header logging in a proxy. The Upferry flow trusts forwarded protocol headers from its reverse proxy and validates the app hostname. On another host set `FORWARDED_ALLOW_IPS` only for your trusted proxy network and do not publish a direct container port with unrestricted forwarded-header trust.

## Data and background jobs

- Authenticated requests use the verified shop returned by the official package. Job IDs are always scoped to that shop.
- Access and refresh tokens are encrypted together using Fernet. Keep `TOKEN_ENCRYPTION_KEY` in your secret storage and with your disaster-recovery material. Losing it prevents decryption. Changing it without re-encrypting stored tokens requires reauthorization.
- Fresh token pairs commit before subsequent GraphQL calls. Refresh uses a Postgres row lock and the SDK's current expiry rules, including its retry recovery behavior.
- Jobs use a Postgres queue with leases, three attempts and recovery after worker termination. Execution is at least once. The sample is read-only. Add idempotency before implementing merchant-side writes, payments or external side effects, and add heartbeats for tasks longer than the lease.
- Uninstall and shop-erasure webhooks remove the installation and jobs. Signed payload identity is checked against the header shop before deletion. The sample stores no customer records; customer data-request/redaction handlers acknowledge that case. Extend their behavior before adding customer data. The starter is not a compliance certification.
- Only scoped read-only shop metadata is queried by the sample. Add scopes and merchant consent deliberately for your actual app.
- Use Alembic migrations when changing the schema. `alembic upgrade head` runs before the web starts; the worker starts afterwards.

## Other hosts

Build `Dockerfile` and use the same image for `python -m app.web` and `python -m app.worker`. Provide Postgres and the variables in `.env.example`, run `alembic upgrade head` once, and publish your Shopify configuration with your host URL. Keep the database private and the web endpoint on HTTPS.

Upferry deployment convenience does not require a proprietary app framework. Hosting terms and resource limits are separate from the free source code.

## Development checks

```sh
uv sync
docker compose up -d
TEST_DATABASE_URL=postgresql://kit:kit@localhost:54327/kit uv run pytest
```

Tests use a disposable schema in the specified Postgres database, real SDK verification and mocked Shopify HTTP responses. Never point them at production.

Official references: [Python package](https://github.com/Shopify/shopify-app-python), [libraries and templates](https://shopify.dev/docs/api/libraries-and-templates), [deployment](https://shopify.dev/docs/apps/launch/deployment).

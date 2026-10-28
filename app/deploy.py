"""Guided Upferry deployment. Credentials stay out of the source archive and logs."""
import argparse
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import tarfile
import tempfile
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import tomlkit
from cryptography.fernet import Fernet
from dotenv import dotenv_values

DEFAULT_API = "https://api.upferry.com"
CONFIG = Path.home() / ".upferry" / "config.json"
STATE = ".upferry-shopify.json"
SKIP = {".git", ".venv", ".shopify", "node_modules", "__pycache__", ".pytest_cache", "dist", ".idea"}


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".launch-kit-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(value, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


class Upferry:
    def __init__(self, base, token=None, client=None):
        parsed = urlsplit(base)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("The Upferry API must use HTTPS (localhost is allowed for tests)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an API origin without credentials or query parameters")
        self.base = base.rstrip("/")
        self.token = token
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)

    def request(self, method, path, body=None, content=None, headers=None):
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.client.request(method, self.base + path, json=body, content=content, headers={**auth, **(headers or {})})
        if not response.is_success:
            # Never echo a response body or variables endpoint: it may contain secrets.
            raise RuntimeError(f"Upferry {method} {path.split('?')[0]} returned HTTP {response.status_code}. Check the project in the panel.")
        return response.json() if response.content else None

    def login(self):
        if self.token:
            response = self.client.get(self.base + "/auth/me", headers={"Authorization": f"Bearer {self.token}"})
            if response.is_success:
                return
            if response.status_code != 401:
                raise RuntimeError("Could not check your Upferry session. Try again when the API is available.")
        self.token = None
        secret = secrets.token_hex(32)
        started = self.request("POST", "/cli/login", {"secretHash": hashlib.sha256(secret.encode()).hexdigest(), "hostname": socket.gethostname()})
        print(f"Confirm Upferry sign-in, code {started['userCode']}: {started['url']}")
        webbrowser.open(started["url"])
        for _ in range(150):
            response = self.client.post(self.base + "/cli/login/poll", json={"id": started["id"], "secret": secret})
            if response.status_code == 202:
                time.sleep(2)
                continue
            if not response.is_success or not response.json().get("token"):
                raise RuntimeError("Upferry sign-in expired or was declined. Run the command again.")
            self.token = response.json()["token"]
            config = read_json(CONFIG)
            # Preserve links used by the regular Upferry CLI.
            config.update({"apiUrl": self.base, "token": self.token, "login": response.json().get("user", {}).get("login")})
            save_private(CONFIG, config)
            return
        raise RuntimeError("Upferry sign-in timed out. Run the command again.")

    def wait(self, deployment_id):
        for _ in range(300):
            deployment = self.request("GET", f"/deployments/{deployment_id}")
            if deployment["status"] == "success":
                return
            if deployment["status"] in {"failed", "cancelled", "crashed", "removed"}:
                raise RuntimeError(f"Deployment {deployment_id} did not succeed. Inspect its build log in Upferry.")
            time.sleep(2)
        raise RuntimeError(f"Deployment {deployment_id} is still pending. Inspect it in Upferry before trying again.")


def source_archive(root, role):
    """No symlinks, credentials, local state or virtual environments are uploaded."""
    excluded = set()
    try:
        ignored = subprocess.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=root, capture_output=True, check=True)
        excluded.update(ignored.stdout.decode().split("\0"))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if path.is_symlink() or not path.is_file() or any(part in SKIP for part in rel.parts):
                continue
            if any(part.startswith(".env") and part != ".env.example" for part in rel.parts) or rel.name == STATE or (rel.name.startswith("shopify.app.") and rel.name != "shopify.app.toml" and rel.suffix == ".toml") or str(rel) in excluded or rel.suffix in {".pem", ".key", ".pyc"}:
                continue
            if str(rel) == "upferry.json":
                continue
            archive.add(path, arcname=str(rel), recursive=False)
        deploy = {"startCommand": f"python -m app.{role}", "restartPolicyType": "ON_FAILURE"}
        if role == "web":
            deploy.update({"preDeployCommand": "alembic upgrade head", "healthcheckPath": "/health"})
        value = json.dumps({"deploy": deploy}).encode()
        entry = tarfile.TarInfo("upferry.json")
        entry.size = len(value)
        entry.mode = 0o644
        archive.addfile(entry, io.BytesIO(value))
    return buffer.getvalue()


def release_config(root, name):
    path = root / f"shopify.app.{name}.toml"
    if not path.exists():
        raise ValueError(f"Link a separate release app first: npx @shopify/cli app config link --config {name}")
    release = tomlkit.parse(path.read_text())
    if not release.get("client_id"):
        raise ValueError("The release config needs a Shopify client_id")
    dev_path = root / "shopify.app.toml"
    dev = tomlkit.parse(dev_path.read_text()) if dev_path.exists() else {}
    if dev.get("client_id") and dev["client_id"] == release["client_id"]:
        raise ValueError("Use a separate Shopify app for release. Local app dev must not change the hosted app's URLs.")
    return path, release


def credentials(root, config_name, release):
    values = dict(dotenv_values(root / ".env"))
    values.update({key: os.environ[key] for key in ["SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "SHOPIFY_OLD_API_SECRET", "TOKEN_ENCRYPTION_KEY"] if key in os.environ})
    if os.getenv("SHOPIFY_API_KEY") and os.environ["SHOPIFY_API_KEY"] != release["client_id"]:
        raise ValueError("SHOPIFY_API_KEY in your environment does not match the release app")
    if not values.get("SHOPIFY_API_SECRET") or values.get("SHOPIFY_API_KEY") != release["client_id"]:
        print("Reading release credentials through Shopify CLI (values are not printed).")
        command = ["npx", "--yes", "@shopify/cli", "app", "env", "show", "--config", config_name]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Sign in and link the release app first: npx @shopify/cli app config link --config {config_name}")
        values.update(dotenv_values(stream=io.StringIO(result.stdout)))
    if values.get("SHOPIFY_API_KEY") != release["client_id"]:
        raise ValueError("The supplied Shopify credentials do not match the release app. Do not deploy development credentials.")
    if not values.get("SHOPIFY_API_SECRET"):
        raise ValueError("Shopify CLI did not return SHOPIFY_API_SECRET")
    if not values.get("TOKEN_ENCRYPTION_KEY"):
        if (root / STATE).exists():
            raise ValueError("Restore TOKEN_ENCRYPTION_KEY from your secret storage before redeploying. Generating a new key would make existing tokens unreadable.")
        values["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        env_path = root / ".env"
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as output:
            output.write("\nTOKEN_ENCRYPTION_KEY=" + values["TOKEN_ENCRYPTION_KEY"] + "\n")
        os.chmod(env_path, 0o600)
    Fernet(values["TOKEN_ENCRYPTION_KEY"].encode())
    return values


def deploy(root, api, values, config_name, project_name):
    config_path, release = release_config(root, config_name)
    state_path = root / STATE
    state = read_json(state_path)
    if state and (state.get("apiUrl") != api.base or state.get("clientId") != release["client_id"]):
        raise ValueError("This folder is linked to a different Upferry API or Shopify app. Use a separate folder.")
    state.update({"apiUrl": api.base, "clientId": release["client_id"]})
    if not state.get("projectId"):
        project = api.request("POST", "/projects", {"name": project_name})
        state["projectId"] = project["id"]
        save_private(state_path, state)
    project_id = state["projectId"]
    api.request("GET", f"/projects/{project_id}")
    if not state.get("databaseId"):
        db = api.request("POST", f"/projects/{project_id}/databases", {"engine": "postgres"})
        state.update({"databaseId": db["service"]["id"], "databaseName": db["service"]["name"], "databaseDeployment": db["deployment"]["id"]})
        save_private(state_path, state)
    print("Waiting for Postgres...")
    api.wait(state["databaseDeployment"])
    for role in ["web", "worker"]:
        if not state.get(role + "Id"):
            created = api.request("POST", f"/projects/{project_id}/services", {"name": role})
            state[role + "Id"] = created["id"]
            save_private(state_path, state)
    web = api.request("GET", f"/services/{state['webId']}")
    url = next(domain["url"] for domain in web["domains"] if domain["kind"] == "generated")
    shared = {"DATABASE_URL": "${{" + state["databaseName"] + ".DATABASE_URL}}", "SHOPIFY_API_KEY": values["SHOPIFY_API_KEY"], "SHOPIFY_API_SECRET": values["SHOPIFY_API_SECRET"], "TOKEN_ENCRYPTION_KEY": values["TOKEN_ENCRYPTION_KEY"], "SHOPIFY_APP_URL": url, "SHOPIFY_API_VERSION": str(release.get("webhooks", {}).get("api_version", "2026-10")), "FORWARDED_ALLOW_IPS": "*"}
    if values.get("SHOPIFY_OLD_API_SECRET"):
        shared["SHOPIFY_OLD_API_SECRET"] = values["SHOPIFY_OLD_API_SECRET"]
    for role in ["web", "worker"]:
        service_id = state[role + "Id"]
        api.request("PUT", f"/services/{service_id}/variables", {"variables": shared})
        print(f"Building and starting {role}...")
        uploaded = api.request("POST", f"/services/{service_id}/up", content=source_archive(root, role), headers={"content-type": "application/gzip"})
        api.wait(uploaded["id"])
    release["application_url"] = url
    release.setdefault("build", tomlkit.table())["automatically_update_urls_on_dev"] = False
    # This starter uses managed installation and token exchange, not a custom OAuth callback.
    release.setdefault("auth", tomlkit.table())["redirect_urls"] = []
    source = tomlkit.parse((root / "shopify.app.toml").read_text())
    release["webhooks"] = source["webhooks"]
    config_path.write_text(tomlkit.dumps(release))
    print(f"App, Postgres and worker are running: {url}")
    print(f"Release configuration updated: {config_path.name}")
    print("Review and publish this Shopify configuration. Shopify CLI will show the proposed changes.")
    return url


def main():
    parser = argparse.ArgumentParser(description="Deploy the Shopify Python starter to Upferry")
    parser.add_argument("--api-url", default=os.getenv("UPFERRY_API_URL", DEFAULT_API))
    parser.add_argument("--config", default="release", help="Named Shopify release config; separate from local development")
    parser.add_argument("--name", help="New Upferry project name")
    parser.add_argument("--skip-shopify-release", action="store_true", help="Leave Shopify configuration publication for a separate command")
    args = parser.parse_args()
    root = Path.cwd()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", args.config):
        parser.error("Use a simple config name such as release")
    if not (root / "app/main.py").is_file() or not (root / "pyproject.toml").is_file():
        parser.error("Run this command from your Shopify Python Launch Kit folder")
    _, release = release_config(root, args.config)
    values = credentials(root, args.config, release)
    config = read_json(CONFIG)
    base = args.api_url.rstrip("/")
    same_api = config.get("apiUrl", DEFAULT_API).rstrip("/") == base
    api = Upferry(base, os.getenv("UPFERRY_TOKEN") or (config.get("token") if same_api else None))
    api.login()
    deploy(root, api, values, args.config, args.name or root.name[:64])
    if not args.skip_shopify_release:
        result = subprocess.run(["npx", "--yes", "@shopify/cli", "app", "deploy", "--config", args.config], cwd=root)
        if result.returncode:
            raise RuntimeError("Hosting is running. Review the Shopify CLI error and publish the release configuration when ready.")
    print("Next: open this release app in Shopify Dev Dashboard, install it on a development store, then run the Python job in its app page.")
    print("For local development use: npx @shopify/cli app dev --config shopify.app.toml")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        # Only our value/runtime messages are printed. HTTP exceptions can contain URLs.
        print(str(error) if isinstance(error, (ValueError, RuntimeError)) else "Network request failed. Check your connection and try again.")
        raise SystemExit(1)

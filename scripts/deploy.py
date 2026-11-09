#!/usr/bin/env python3
"""
Single deploy tool for mauxy. Builds/pushes the image, syncs the k8s Secret, and
renders + applies the k8s manifests — all from `.env` values (no secrets in git).

Usage:
    python scripts/deploy.py --dry-run             # print rendered manifests
    python scripts/deploy.py                        # render to k8s/rendered/
    python scripts/deploy.py --build                # docker login + build + push
    python scripts/deploy.py --secret               # sync mauxy-credentials Secret
    python scripts/deploy.py --build --secret --apply   # full prod deploy

Config comes from `.env` (see `.env.example`), overridable by real env vars.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# -- .env loader (no external deps) -------------------------------------------

def load_dotenv(path: Path) -> dict:
    """Parse a .env file into a dict. Skips comments and blank lines."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


# Every var an action may read — overlaid from real env vars so CI / a secrets
# manager can inject them without writing a .env.
KNOWN_VARS = [
    "DEPLOY_NAMESPACE", "DEPLOY_IMAGE", "DEPLOY_DOMAIN", "DEPLOY_IMAGE_PULL_SECRET",
    "REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD",
    "MAUTIC_BASE_URL", "MAUTIC_USERNAME", "MAUTIC_PASSWORD", "MAUTIC_VERSION",
    "ALLOWED_ORIGINS", "MAUXY_SITES", "CHALLENGE_SECRET", "CHALLENGE_TTL", "RATE_LIMIT",
    "ACTION_LOG_DB", "ADMIN_API_KEY",
] + [  # website consent service (DOI, sinks, self-check, leads, login; flywheel SPEC-020/021)
    *(k for k in (
        "MIGRATE_STRICT", "SUBSCRIBE_RATE_LIMIT", "CHALLENGE_RATE_LIMIT", "CONFIRM_RATE_LIMIT",
        "CONFIRM_TOKEN_SECRET", "CONFIRM_TOKEN_MAX_AGE", "PUBLIC_BASE_URL", "PUBLIC_WEBSITE_BASE_URL",
        "NEWSLETTER_PENDING_DAYS", "MAX_PENDING_PER_EMAIL",
        "MAIL_MODE", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
        "SMTP_REPLY_TO", "SMTP_TIMEOUT", "MAIL_MAX_BYTES", "MAIL_ATTACHMENT_TYPES", "MAIL_RECIPIENT_HOURLY_LIMIT",
        "MAIL_INTERNAL_RECIPIENTS", "MAIL_MAX_ATTEMPTS", "MAIL_BACKOFF_SECONDS", "MAIL_RETENTION_DAYS",
        "LEAD_SINK", "SUBSCRIBER_SINK",
        "ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY", "ODOO_TIMEOUT",
        "ODOO_LEAD_USER_ID", "ODOO_LEAD_TEAM_ID", "ODOO_LEAD_TAG_IDS", "ODOO_UTM_SOURCE", "ODOO_UTM_MEDIUM",
        "ODOO_LIST_NEWSLETTER_ID", "ODOO_LIST_RADAR_ID",
        "LEAD_ALLOWED_ORIGINS", "LEAD_WEBSITE_URL", "LEAD_RATE_LIMIT", "LEAD_NOTIFY_TO",
        "LEAD_PENDING_DAYS", "LEAD_RETENTION_DAYS", "LEAD_SYNC_MAX_ATTEMPTS", "LEAD_SYNC_BACKOFF_SECONDS",
        "SHUTDOWN_DRAIN_SECONDS",
        "SELFCHECK_ALLOWED_ORIGINS", "SELFCHECK_WEBSITE_URL", "SELFCHECK_NOTIFY_TO",
        "SELFCHECK_RATE_LIMIT", "SELFCHECK_EVENT_RATE_LIMIT", "SELFCHECK_PENDING_DAYS", "SELFCHECK_RETENTION_DAYS",
        "SELFCHECK_NAME_REQUIRED",
        "MAUTIC_RADAR_TAG", "MAUTIC_RADAR_SEGMENT_ID",
        "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_REDIRECT_URI",
        "AUTH_ALLOWED_ORIGINS", "AUTH_TOKEN_SECRET", "AUTH_SESSION_TTL", "AUTH_RATE_LIMIT",
        "AUTH_SESSION_RATE_LIMIT",
        # per-site brand + routing (db-patches/0006); webhook secrets: WEBHOOK_SECRET_<REF> (pattern below)
        "DEFAULT_SITE", "BRAND_LOGO_MAX_BYTES", "MAIL_SENDER_DOMAINS", "WEBHOOK_ALLOWED_HOSTS", "WEBHOOK_TIMEOUT",
        "OPTOUT_RESIGNUP_DAYS",
    )),
]
# One secret per webhook target (routing.py secret_ref -> WEBHOOK_SECRET_<REF>): any number, synced too.
WEBHOOK_SECRET_RE = re.compile(r"^WEBHOOK_SECRET_[A-Z0-9_]{1,40}$")

RENDER_REQUIRED = ["DEPLOY_NAMESPACE", "DEPLOY_IMAGE", "DEPLOY_DOMAIN", "DEPLOY_IMAGE_PULL_SECRET"]

# Keys written into the mauxy-credentials Secret (the deployment is envFrom this
# Secret, so all runtime config lives here). Listed keys are set when present.
# Every runtime var (deploy-only vars excluded) -- the app reads them all from this Secret.
SECRET_KEYS = [k for k in KNOWN_VARS if not k.startswith(("DEPLOY_", "REGISTRY_"))]
# Always needed: site keys + quiz (newsletter gate) and the DOI signing key. Mautic creds only
# when the deprecated Mautic subscriber sink is selected (SUBSCRIBER_SINK=mautic).
SECRET_REQUIRED = ["MAUXY_SITES", "CHALLENGE_SECRET", "CONFIRM_TOKEN_SECRET"]
SECRET_REQUIRED_MAUTIC = ["MAUTIC_USERNAME", "MAUTIC_PASSWORD"]

PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _require(variables: dict, keys: list, action: str):
    missing = [k for k in keys if not variables.get(k)]
    if missing:
        _die(f"--{action} needs: {', '.join(missing)} (set them in .env or the environment)")


def render(template: str, variables: dict) -> str:
    """Replace ${VAR} placeholders with values from variables dict."""
    def replacer(match):
        key = match.group(1)
        return variables.get(key, match.group(0))   # leave unknown placeholders intact
    return PLACEHOLDER_RE.sub(replacer, template)


# -- Actions ------------------------------------------------------------------

def do_build(variables: dict):
    """docker login (creds from .env), build the image, push it."""
    _require(variables, ["REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD", "DEPLOY_IMAGE"], "build")
    reg, user = variables["REGISTRY_URL"], variables["REGISTRY_USER"]
    image = variables["DEPLOY_IMAGE"]
    print(f"==> docker login {reg}")
    subprocess.run(["docker", "login", reg, "-u", user, "--password-stdin"],
                   input=variables["REGISTRY_PASSWORD"].encode(), check=True)
    print(f"==> docker build -t {image}")
    subprocess.run(["docker", "build", "-t", image, str(PROJECT_ROOT)], check=True)
    print(f"==> docker push {image}")
    subprocess.run(["docker", "push", image], check=True)


def do_secret(variables: dict):
    """Sync the mauxy-credentials Secret from .env (apply via kubectl)."""
    required = ["DEPLOY_NAMESPACE"] + SECRET_REQUIRED
    if variables.get("SUBSCRIBER_SINK", "odoo").strip().lower() == "mautic":
        required += SECRET_REQUIRED_MAUTIC
    _require(variables, required, "secret")
    ns = variables["DEPLOY_NAMESPACE"]
    args = ["kubectl", "create", "secret", "generic", "mauxy-credentials", "-n", ns,
            "--dry-run=client", "-o", "yaml"]
    for key in SECRET_KEYS + sorted(k for k in variables if WEBHOOK_SECRET_RE.match(k)):
        if variables.get(key):
            args += [f"--from-literal={key}={variables[key]}"]
    print(f"==> sync Secret mauxy-credentials (ns {ns})")
    create = subprocess.run(args, check=True, capture_output=True)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=create.stdout, check=True)


def do_render(variables: dict, *, dry_run: bool, apply: bool):
    """Render k8s manifests, then write to k8s/rendered/ (and apply if asked)."""
    _require(variables, RENDER_REQUIRED, "apply" if apply else "render")
    # Only *.yaml is applied. The Secret is synced separately by --secret (do_secret);
    # the documentation template lives at k8s/secret.yaml.example (note the .example
    # suffix) so it is NOT matched here and can never clobber the real mauxy-credentials.
    templates = sorted((PROJECT_ROOT / "k8s").glob("*.yaml"))
    if not templates:
        _die("No .yaml templates found in k8s/")

    if dry_run:
        for tpl in templates:
            print(f"# --- {tpl.name} ---")
            print(render(tpl.read_text(), variables))
            print()
        return

    rendered_dir = PROJECT_ROOT / "k8s" / "rendered"
    rendered_dir.mkdir(exist_ok=True)
    for tpl in templates:
        out = rendered_dir / tpl.name
        out.write_text(render(tpl.read_text(), variables))
        print(f"  rendered: {out.relative_to(PROJECT_ROOT)}")

    if apply:
        print(f"\n==> kubectl apply -f {rendered_dir.relative_to(PROJECT_ROOT)}/")
        sys.exit(subprocess.run(["kubectl", "apply", "-f", str(rendered_dir)]).returncode)
    print(f"\nManifests written to {rendered_dir.relative_to(PROJECT_ROOT)}/ "
          "(run with --apply to kubectl apply).")


# -- Main ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Build, secret-sync, and apply mauxy to k8s.")
    p.add_argument("--build", action="store_true", help="docker login + build + push DEPLOY_IMAGE.")
    p.add_argument("--secret", action="store_true", help="Sync the mauxy-credentials Secret from .env.")
    p.add_argument("--apply", action="store_true", help="Render to k8s/rendered/ and kubectl apply.")
    p.add_argument("--dry-run", action="store_true", help="Print rendered manifests to stdout.")
    p.add_argument("--env-file", default=".env", help="Path to .env (default: .env in project root).")
    args = p.parse_args()

    env_path = Path(args.env_file) if os.path.isabs(args.env_file) else PROJECT_ROOT / args.env_file
    variables = load_dotenv(env_path)
    for key in KNOWN_VARS + [k for k in os.environ if WEBHOOK_SECRET_RE.match(k)]:  # real env vars win over .env
        if os.environ.get(key):
            variables[key] = os.environ[key]

    if args.build:
        do_build(variables)
    if args.secret:
        do_secret(variables)
    if args.dry_run:
        do_render(variables, dry_run=True, apply=False)
    elif args.apply:
        do_render(variables, dry_run=False, apply=True)
    elif not (args.build or args.secret):
        do_render(variables, dry_run=False, apply=False)   # default: render only


if __name__ == "__main__":
    main()

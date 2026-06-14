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
]

RENDER_REQUIRED = ["DEPLOY_NAMESPACE", "DEPLOY_IMAGE", "DEPLOY_DOMAIN", "DEPLOY_IMAGE_PULL_SECRET"]

# Keys written into the mauxy-credentials Secret (the deployment is envFrom this
# Secret, so all runtime config lives here). Listed keys are set when present.
SECRET_KEYS = [
    "MAUTIC_BASE_URL", "MAUTIC_USERNAME", "MAUTIC_PASSWORD", "MAUTIC_VERSION",
    "ALLOWED_ORIGINS", "MAUXY_SITES", "CHALLENGE_SECRET", "CHALLENGE_TTL", "RATE_LIMIT",
    "ACTION_LOG_DB", "ADMIN_API_KEY",
]
SECRET_REQUIRED = ["MAUTIC_USERNAME", "MAUTIC_PASSWORD", "MAUXY_SITES", "CHALLENGE_SECRET"]

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
    _require(variables, ["DEPLOY_NAMESPACE"] + SECRET_REQUIRED, "secret")
    ns = variables["DEPLOY_NAMESPACE"]
    args = ["kubectl", "create", "secret", "generic", "mauxy-credentials", "-n", ns,
            "--dry-run=client", "-o", "yaml"]
    for key in SECRET_KEYS:
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
    for key in KNOWN_VARS:                       # real env vars win over .env
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

#!/usr/bin/env python3
"""
Render k8s manifest templates by substituting ${VAR} placeholders
with values from .env, then optionally apply them via kubectl.

Usage:
    python scripts/deploy.py --dry-run      # print rendered manifests to stdout
    python scripts/deploy.py                # render to k8s/rendered/
    python scripts/deploy.py --apply        # render and kubectl apply
"""

import argparse
import os
import re
import sys
from pathlib import Path

# -- .env loader (no external deps) -------------------------------------------

def load_dotenv(path: Path) -> dict:
    """Parse a .env file into a dict. Skips comments and blank lines."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


# -- Template rendering --------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

REQUIRED_VARS = [
    "DEPLOY_NAMESPACE",
    "DEPLOY_IMAGE",
    "DEPLOY_DOMAIN",
    "DEPLOY_IMAGE_PULL_SECRET",
]


def render(template: str, variables: dict) -> str:
    """Replace ${VAR} placeholders with values from variables dict."""
    def replacer(match):
        key = match.group(1)
        if key in variables:
            return variables[key]
        return match.group(0)  # leave unknown placeholders intact
    return PLACEHOLDER_RE.sub(replacer, template)


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Render k8s templates from .env and optionally apply them."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print rendered manifests to stdout instead of writing files."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Render to k8s/rendered/ and run kubectl apply -f."
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="Path to .env file (default: .env in project root)."
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file) if os.path.isabs(args.env_file) else project_root / args.env_file

    # Load variables: .env values, overridden by actual env vars
    variables = load_dotenv(env_path)
    for key in REQUIRED_VARS:
        env_val = os.environ.get(key)
        if env_val:
            variables[key] = env_val

    # Validate required vars
    missing = [v for v in REQUIRED_VARS if not variables.get(v)]
    if missing:
        print(f"ERROR: Missing required variables: {', '.join(missing)}", file=sys.stderr)
        print(f"Set them in {env_path} or as environment variables.", file=sys.stderr)
        sys.exit(1)

    # Discover templates
    k8s_dir = project_root / "k8s"
    templates = sorted(k8s_dir.glob("*.yaml"))
    if not templates:
        print("ERROR: No .yaml templates found in k8s/", file=sys.stderr)
        sys.exit(1)

    rendered_dir = k8s_dir / "rendered"

    if args.dry_run:
        # Print to stdout
        for tpl in templates:
            content = tpl.read_text()
            rendered = render(content, variables)
            print(f"# --- {tpl.name} ---")
            print(rendered)
            print()
        return

    # Write to k8s/rendered/
    rendered_dir.mkdir(exist_ok=True)
    rendered_files = []
    for tpl in templates:
        content = tpl.read_text()
        rendered = render(content, variables)
        out_path = rendered_dir / tpl.name
        out_path.write_text(rendered)
        rendered_files.append(out_path)
        print(f"  rendered: {out_path.relative_to(project_root)}")

    if args.apply:
        import subprocess
        cmd = ["kubectl", "apply", "-f", str(rendered_dir)]
        print(f"\n  running: {' '.join(cmd)}")
        result = subprocess.run(cmd)
        sys.exit(result.returncode)
    else:
        print(f"\nManifests written to {rendered_dir.relative_to(project_root)}/")
        print("Run with --apply to kubectl apply, or: kubectl apply -f k8s/rendered/")


if __name__ == "__main__":
    main()

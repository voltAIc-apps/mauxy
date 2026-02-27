#!/usr/bin/env python3
"""
Diagnostic script to isolate DNC failure point (issue #5).

Replays the full unsubscribe flow step-by-step against the live Mautic API,
printing complete request/response details at each stage.

Usage:
    python diagnose.py <email> [--base-url URL] [--username USER] [--password PASS]

Credentials resolve: CLI flag > env var > default from .env.example.
"""

import argparse
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field

import httpx

# ── Data classes ─────────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

SUSPECT_LABELS = {
    "A": "DNC endpoint returns 200 but body contains errors",
    "B": "DNC not persisted after successful response",
    "F": "Idempotency issue on repeated DNC add",
}


@dataclass
class StepResult:
    step_num: int
    name: str
    status: str  # PASS / FAIL / WARN / SKIP
    notes: str = ""
    suspects_confirmed: list = field(default_factory=list)
    suspects_cleared: list = field(default_factory=list)


@dataclass
class DiagContext:
    email: str
    base_url: str
    auth: tuple
    client: httpx.Client
    contact_id: str | None = None
    search_body: dict | None = None
    pre_dnc_state: dict | None = None
    post_dnc_state: dict | None = None
    step5_status: int | None = None
    step5_body: dict | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def banner(step_num: int, name: str):
    print(f"\n{'=' * 70}")
    print(f"  STEP {step_num}: {name}")
    print(f"{'=' * 70}")


def print_response(resp: httpx.Response):
    print(f"  URL:     {resp.request.url}")
    print(f"  Method:  {resp.request.method}")
    print(f"  Status:  {resp.status_code}")
    elapsed = resp.elapsed.total_seconds() if resp.elapsed else "?"
    print(f"  Elapsed: {elapsed}s")
    try:
        body = resp.json()
        print(f"  Body:\n{textwrap.indent(json.dumps(body, indent=2), '    ')}")
    except Exception:
        print(f"  Body (raw): {resp.text[:2000]}")


def has_email_dnc(contact_data: dict) -> bool:
    """Check if a contact's data contains an email DNC entry."""
    dnc_list = contact_data.get("doNotContact", [])
    for entry in dnc_list:
        if entry.get("channel") == "email":
            return True
    return False


def result_icon(status: str) -> str:
    icons = {PASS: "+", FAIL: "!", WARN: "~", SKIP: "-"}
    return icons.get(status, "?")


# ── Steps ────────────────────────────────────────────────────────────────────

def step1_connectivity(ctx: DiagContext) -> StepResult:
    banner(1, "Connectivity")
    url = f"{ctx.base_url}/api/contacts"
    try:
        resp = ctx.client.get(url, params={"limit": 1}, auth=ctx.auth)
        print_response(resp)
        if resp.status_code == 200:
            return StepResult(1, "Connectivity", PASS, "Mautic reachable, credentials valid")
        elif resp.status_code == 401:
            return StepResult(1, "Connectivity", FAIL, f"Authentication failed (HTTP {resp.status_code})")
        else:
            return StepResult(1, "Connectivity", FAIL, f"Unexpected status {resp.status_code}")
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(1, "Connectivity", FAIL, f"Connection error: {exc}")


def step2_contact_search(ctx: DiagContext) -> StepResult:
    banner(2, "Contact search")
    url = f"{ctx.base_url}/api/contacts"
    params = {
        "where[0][col]": "email",
        "where[0][expr]": "eq",
        "where[0][val]": ctx.email,
    }
    try:
        resp = ctx.client.get(url, params=params, auth=ctx.auth)
        print_response(resp)
        if resp.status_code != 200:
            return StepResult(2, "Contact search", FAIL, f"Search returned HTTP {resp.status_code}")
        body = resp.json()
        ctx.search_body = body
        contacts = body.get("contacts", {})
        count = len(contacts)
        print(f"\n  Contacts returned: {count}")
        if count == 0:
            return StepResult(2, "Contact search", FAIL, "No contacts found for this email")
        return StepResult(2, "Contact search", PASS, f"{count} candidate(s) returned")
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(2, "Contact search", FAIL, f"Request error: {exc}")


def step3_exact_match(ctx: DiagContext, search_resp_body: dict) -> StepResult:
    banner(3, "Exact match")
    contacts = search_resp_body.get("contacts", {})
    if not contacts:
        print("  No candidates to check (step 2 returned none)")
        return StepResult(3, "Exact match", SKIP, "No candidates from step 2")

    for cid, cdata in contacts.items():
        fields = cdata.get("fields", {}).get("core", {})
        contact_email = (fields.get("email", {}).get("value") or "").lower()
        print(f"  Candidate ID={cid}  email={contact_email!r}")
        if contact_email == ctx.email.lower():
            ctx.contact_id = cid
            print(f"  >> Exact match found: contact_id={cid}")
            return StepResult(3, "Exact match", PASS, f"contact_id={cid}")

    print("  No exact email match among candidates")
    return StepResult(3, "Exact match", FAIL, "API returned contacts but none matched exactly")


def step4_pre_dnc_state(ctx: DiagContext) -> StepResult:
    banner(4, "Pre-DNC state")
    if ctx.contact_id is None:
        print("  Skipped — no contact_id from step 3")
        return StepResult(4, "Pre-DNC state", SKIP, "No contact_id")

    url = f"{ctx.base_url}/api/contacts/{ctx.contact_id}"
    try:
        resp = ctx.client.get(url, auth=ctx.auth)
        print_response(resp)
        if resp.status_code != 200:
            return StepResult(4, "Pre-DNC state", FAIL, f"HTTP {resp.status_code}")

        body = resp.json()
        contact = body.get("contact", body)
        ctx.pre_dnc_state = contact
        dnc_list = contact.get("doNotContact", [])
        print(f"\n  doNotContact entries: {json.dumps(dnc_list, indent=2)}")

        if has_email_dnc(contact):
            return StepResult(4, "Pre-DNC state", WARN, "Already on email DNC before add")
        return StepResult(4, "Pre-DNC state", PASS, "Not on email DNC (as expected)")
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(4, "Pre-DNC state", FAIL, f"Request error: {exc}")


def step5_dnc_add(ctx: DiagContext) -> StepResult:
    banner(5, "DNC add")
    if ctx.contact_id is None:
        print("  Skipped — no contact_id from step 3")
        return StepResult(5, "DNC add", SKIP, "No contact_id")

    url = f"{ctx.base_url}/api/contacts/{ctx.contact_id}/dnc/email/add"
    payload = {"reason": 1, "comments": "Unsubscribed via website (diagnose.py)"}
    try:
        resp = ctx.client.post(url, json=payload, auth=ctx.auth)
        print_response(resp)
        ctx.step5_status = resp.status_code
        try:
            ctx.step5_body = resp.json()
        except Exception:
            ctx.step5_body = {}

        notes_parts = []
        suspects_confirmed = []
        suspects_cleared = []

        # Check for errors key even on 200
        errors = ctx.step5_body.get("errors") or ctx.step5_body.get("error")
        if errors:
            print(f"\n  !! ERRORS in response body despite HTTP {resp.status_code}: {errors}")
            notes_parts.append(f"Body contains errors: {errors}")
            suspects_confirmed.append("A")
        else:
            suspects_cleared.append("A")

        if resp.status_code in (200, 201):
            status = PASS if not errors else FAIL
            notes_parts.insert(0, f"HTTP {resp.status_code}")
        else:
            status = FAIL
            notes_parts.insert(0, f"HTTP {resp.status_code}")

        return StepResult(5, "DNC add", status, "; ".join(notes_parts),
                          suspects_confirmed=suspects_confirmed,
                          suspects_cleared=suspects_cleared)
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(5, "DNC add", FAIL, f"Request error: {exc}")


def step6_post_dnc_verify(ctx: DiagContext) -> StepResult:
    banner(6, "Post-DNC verify")
    if ctx.contact_id is None:
        print("  Skipped — no contact_id from step 3")
        return StepResult(6, "Post-DNC verify", SKIP, "No contact_id")

    url = f"{ctx.base_url}/api/contacts/{ctx.contact_id}"
    try:
        resp = ctx.client.get(url, auth=ctx.auth)
        print_response(resp)
        if resp.status_code != 200:
            return StepResult(6, "Post-DNC verify", FAIL, f"HTTP {resp.status_code}")

        body = resp.json()
        contact = body.get("contact", body)
        ctx.post_dnc_state = contact
        dnc_list = contact.get("doNotContact", [])
        print(f"\n  doNotContact entries: {json.dumps(dnc_list, indent=2)}")

        # Compare before/after
        pre_had_dnc = has_email_dnc(ctx.pre_dnc_state) if ctx.pre_dnc_state else None
        post_has_dnc = has_email_dnc(contact)

        print(f"  Pre-DNC had email DNC:  {pre_had_dnc}")
        print(f"  Post-DNC has email DNC: {post_has_dnc}")

        suspects_confirmed = []
        suspects_cleared = []

        if post_has_dnc:
            suspects_cleared.append("B")
            return StepResult(6, "Post-DNC verify", PASS,
                              "DNC persisted — email channel present",
                              suspects_cleared=suspects_cleared)
        else:
            suspects_confirmed.append("B")
            return StepResult(6, "Post-DNC verify", FAIL,
                              "DNC NOT persisted — email channel missing after add",
                              suspects_confirmed=suspects_confirmed)
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(6, "Post-DNC verify", FAIL, f"Request error: {exc}")


def step7_idempotency(ctx: DiagContext) -> StepResult:
    banner(7, "Idempotency (re-add DNC)")
    if ctx.contact_id is None:
        print("  Skipped — no contact_id from step 3")
        return StepResult(7, "Idempotency", SKIP, "No contact_id")

    url = f"{ctx.base_url}/api/contacts/{ctx.contact_id}/dnc/email/add"
    payload = {"reason": 1, "comments": "Unsubscribed via website (diagnose.py re-add)"}
    try:
        resp = ctx.client.post(url, json=payload, auth=ctx.auth)
        print_response(resp)

        try:
            body = resp.json()
        except Exception:
            body = {}

        suspects_confirmed = []
        suspects_cleared = []
        notes_parts = [f"HTTP {resp.status_code}"]

        # Compare with step 5
        if ctx.step5_status is not None:
            if resp.status_code != ctx.step5_status:
                notes_parts.append(f"Status differs from step 5 ({ctx.step5_status} -> {resp.status_code})")

        errors = body.get("errors") or body.get("error")
        if errors:
            print(f"\n  !! ERRORS on re-add: {errors}")
            notes_parts.append(f"Errors on re-add: {errors}")
            suspects_confirmed.append("F")
        else:
            suspects_cleared.append("F")

        if resp.status_code in (200, 201) and not errors:
            status = PASS
        elif errors:
            status = WARN
        else:
            status = WARN
            notes_parts.append("Non-2xx on re-add")

        return StepResult(7, "Idempotency", status, "; ".join(notes_parts),
                          suspects_confirmed=suspects_confirmed,
                          suspects_cleared=suspects_cleared)
    except httpx.RequestError as exc:
        print(f"  ERROR: {exc}")
        return StepResult(7, "Idempotency", FAIL, f"Request error: {exc}")


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary(results: list[StepResult]):
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"  {'STEP':<6} {'NAME':<25} {'STATUS':<8} NOTES")
    print(f"  {'----':<6} {'----':<25} {'------':<8} -----")
    for r in results:
        icon = result_icon(r.status)
        line = f"  {r.step_num:<6} {r.name:<25} [{icon}] {r.status:<4}"
        if r.notes:
            line += f"   {r.notes}"
        print(line)

    all_confirmed = set()
    all_cleared = set()
    for r in results:
        all_confirmed.update(r.suspects_confirmed)
        all_cleared.update(r.suspects_cleared)
    # Don't list a suspect as cleared if it was also confirmed
    all_cleared -= all_confirmed

    print()
    if all_confirmed:
        labels = ", ".join(f"{s} ({SUSPECT_LABELS.get(s, '?')})" for s in sorted(all_confirmed))
        print(f"  SUSPECTS CONFIRMED: {labels}")
    else:
        print("  SUSPECTS CONFIRMED: (none)")
    if all_cleared:
        labels = ", ".join(f"{s} ({SUSPECT_LABELS.get(s, '?')})" for s in sorted(all_cleared))
        print(f"  SUSPECTS CLEARED:   {labels}")
    else:
        print(f"  SUSPECTS CLEARED:   (none)")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Diagnose Mautic DNC failures — replays the full unsubscribe flow step by step.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Steps:
              1  Connectivity     — Can we reach Mautic? Are credentials valid?
              2  Contact search   — Does the exact-match query return results?
              3  Exact match      — Verify email match among candidates.
              4  Pre-DNC state    — Inspect doNotContact BEFORE the add.
              5  DNC add          — Add to DNC; print full response body.
              6  Post-DNC verify  — Confirm DNC persisted after add.
              7  Idempotency      — Re-add DNC and compare response.

            Credentials resolve: CLI flag > env var > default.
        """),
    )
    parser.add_argument("email", help="Email address to diagnose")
    parser.add_argument("--base-url", default=None, help="Mautic base URL (default: env MAUTIC_BASE_URL)")
    parser.add_argument("--username", default=None, help="Mautic API username (default: env MAUTIC_USERNAME)")
    parser.add_argument("--password", default=None, help="Mautic API password (default: env MAUTIC_PASSWORD)")
    args = parser.parse_args()

    base_url = (args.base_url or os.environ.get("MAUTIC_BASE_URL", "")).rstrip("/")
    username = args.username or os.environ.get("MAUTIC_USERNAME", "")
    password = args.password or os.environ.get("MAUTIC_PASSWORD", "")

    email = args.email.lower().strip()

    print(f"{'=' * 70}")
    print(f"  Mautic DNC Diagnostic — issue #5")
    print(f"{'=' * 70}")
    print(f"  Email:    {email}")
    print(f"  Base URL: {base_url}")
    print(f"  Username: {username or '[NOT SET]'}")
    print(f"  Password: {'[SET]' if password else '[NOT SET]'}")
    print()

    if not username or not password:
        print("ERROR: Mautic credentials not set. Use --username/--password or env vars.")
        sys.exit(1)

    auth = (username, password)
    client = httpx.Client(timeout=15.0)
    ctx = DiagContext(email=email, base_url=base_url, auth=auth, client=client)
    results: list[StepResult] = []

    # Step 1
    r1 = step1_connectivity(ctx)
    results.append(r1)

    # Step 2
    r2 = step2_contact_search(ctx)
    results.append(r2)

    # Step 3 — uses search body from step 2
    r3 = step3_exact_match(ctx, ctx.search_body or {})
    results.append(r3)

    # Steps 4-7 depend on contact_id
    r4 = step4_pre_dnc_state(ctx)
    results.append(r4)

    r5 = step5_dnc_add(ctx)
    results.append(r5)

    r6 = step6_post_dnc_verify(ctx)
    results.append(r6)

    r7 = step7_idempotency(ctx)
    results.append(r7)

    client.close()

    print_summary(results)

    # Exit code: non-zero if any step failed
    if any(r.status == FAIL for r in results):
        sys.exit(2)


if __name__ == "__main__":
    main()

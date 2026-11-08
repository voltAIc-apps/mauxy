"""
LinkedIn login (auth_api.py), T-AUTH-0xx. LinkedIn is ALWAYS mocked (respx): token endpoint and
userinfo. Cases in tests/cases/auth_cases.json (return paths, Referer -> site origin, config
problems, LinkedIn error passthrough); flow tests below (state single use, cookie binding, code
single use + binding + origin + TTL, no PII in URLs, nothing long-lived stored).
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import auth_api

CASES = json.loads((Path(__file__).parent / "cases" / "auth_cases.json").read_text())
SITE = "https://site.test"
BINDING = "b" * 43
REDIRECT_URI = "https://mauxy.test/api/auth/linkedin/callback"
PROFILE = {"sub": "li-abc123", "name": "Erika Muster", "given_name": "Erika", "family_name": "Muster",
           "email": "Erika@Example.com", "email_verified": True, "picture": "https://media.licdn.com/x.jpg",
           "locale": {"country": "DE", "language": "de"}}


@pytest.fixture
def ac(app):
    """Client on mauxy's public https host (the __Host- cookie needs https + the redirect host)."""
    with TestClient(app, base_url="https://mauxy.test", follow_redirects=False) as c:
        yield c


@pytest.fixture
def linkedin():
    """Mocked LinkedIn: token + userinfo. Tests change the answers via the returned routes."""
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(auth_api.LINKEDIN_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "li-access-token-xyz", "expires_in": 5183999,
                                                   "scope": "openid,profile,email"}))
        info = mock.get(auth_api.LINKEDIN_USERINFO_URL).mock(return_value=httpx.Response(200, json=PROFILE))
        yield {"token": token, "info": info}


def start(ac, ret="/de/kontakt", lang="de", binding=BINDING, referer=None):
    params = {"lang": lang, "binding": binding}
    if ret is not None:
        params["return"] = ret
    headers = {"Referer": referer} if referer else {}
    return ac.get("/api/auth/linkedin/start", params=params, headers=headers)


def state_of(resp) -> str:
    return parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]


def site_params(resp) -> tuple[str, dict]:
    loc = urlsplit(resp.headers["location"])
    return f"{loc.scheme}://{loc.netloc}{loc.path}", {k: v[0] for k, v in parse_qs(loc.query).items()}


def login(ac, **kw):
    """start + callback with LinkedIn's code; returns the callback response (302 to the site)."""
    r = start(ac, **kw)
    assert r.status_code == 302, r.text
    return ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": state_of(r)})


def exchange(ac, code, binding=BINDING, origin=SITE):
    return ac.post("/api/auth/session", json={"code": code, "binding": binding},
                   headers={"Origin": origin} if origin else {})


async def _rows(app, sql):
    async with app.state.db.execute(sql) as cur:
        return [dict(r) for r in await cur.fetchall()]


def rows(ac, sql):
    return ac.portal.call(_rows, ac.app, sql)


# ---------------------------------------------------------------- open redirect: return path

@pytest.mark.parametrize("case", CASES["return"], ids=[c["id"] for c in CASES["return"]])
def test_return_path(case):
    assert auth_api.safe_return(case["return"], case.get("lang", "de")) == case["expected"]


@pytest.mark.parametrize("case", CASES["return"], ids=[c["id"] for c in CASES["return"]])
def test_start_refuses_bad_return(ac, case):
    r = start(ac, ret=case["return"], lang=case.get("lang", "de"))
    if case["expected"] is None:
        assert r.status_code == 400 and r.json() == {"status": "invalid_request"}
        assert "set-cookie" not in r.headers
    else:
        assert r.status_code == 302
        assert urlsplit(r.headers["location"]).netloc == "www.linkedin.com"


@pytest.mark.parametrize("case", CASES["referer"], ids=[c["id"] for c in CASES["referer"]])
def test_site_origin_from_referer(ac, linkedin, case):
    """The Referer only picks among allowlisted origins; look-alikes fall back to the default."""
    cb = login(ac, referer=case["referer"])
    base, q = site_params(cb)
    assert base == case["origin"] + "/de/auth/callback" and "code" in q


@pytest.mark.parametrize("case", CASES["problems"], ids=[c["id"] for c in CASES["problems"]])
def test_config_problems(monkeypatch, case):
    for k, v in case["env"].items():
        monkeypatch.setattr(auth_api, k, v)
    got = auth_api.problems()
    assert len(got) == len(case["problems"]), got
    for name, problem in zip(case["problems"], got):
        assert problem.startswith(name)


def test_T_AUTH_050_start_validates_lang_and_binding(ac):
    assert start(ac, lang="fr").status_code == 400
    assert start(ac, binding="short").status_code == 400
    assert start(ac, binding="x" * 43 + "!").status_code == 400
    assert ac.get("/api/auth/linkedin/start", params={"lang": "de"}).status_code == 400   # binding required


# ---------------------------------------------------------------- start

def test_T_AUTH_051_start_redirects_to_linkedin(ac):
    r = start(ac)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://www.linkedin.com/oauth/v2/authorization"
    assert q["response_type"] == ["code"] and q["client_id"] == ["test-client-id"]
    assert q["redirect_uri"] == [REDIRECT_URI]
    assert q["scope"] == ["openid profile email"]          # sign-in only, never posting scopes
    assert "w_member_social" not in r.headers["location"] and "w_organization" not in r.headers["location"]
    assert q["state"][0]                                     # signed (not encrypted): id, origin, lang, path; no PII


def test_T_AUTH_052_state_cookie_attributes(ac):
    r = start(ac)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-mauxy_li=")
    low = cookie.lower()
    for attr in ("httponly", "secure", "samesite=lax", "path=/", "max-age=600"):
        assert attr in low, attr
    assert "domain=" not in low


def test_T_AUTH_053_state_row_has_no_pii(ac):
    start(ac)
    (row,) = rows(ac, "SELECT * FROM auth_state")
    assert set(row) == {"id", "cookie_hash", "binding_hash", "created_at", "used_at"}
    assert len(row["cookie_hash"]) == 64 and row["binding_hash"] == auth_api._sha(BINDING)


def test_T_AUTH_054_start_on_wrong_host_is_unavailable(app):
    """Cookie would never reach the callback: start must run on the redirect_uri's host."""
    with TestClient(app, base_url="https://other.test", follow_redirects=False) as c:
        r = start(c)
    base, q = site_params(r)
    assert base == SITE + "/de/auth/callback" and q == {"error": "unavailable"}


def test_T_AUTH_055_disabled_login_is_unavailable(ac, monkeypatch):
    monkeypatch.setattr(auth_api, "_PROBLEMS", ["LINKEDIN_CLIENT_ID/LINKEDIN_CLIENT_SECRET unset"])
    _, q = site_params(start(ac))
    assert q == {"error": "unavailable"}
    _, q = site_params(ac.get("/api/auth/linkedin/callback", params={"code": "x", "state": "y"}))
    assert q == {"error": "unavailable"}
    assert exchange(ac, "c" * 43).status_code == 503


# ---------------------------------------------------------------- callback: happy path + PII

def test_T_AUTH_056_login_flow_end_to_end(ac, linkedin):
    cb = login(ac)
    assert cb.status_code == 302 and cb.headers["referrer-policy"] == "no-referrer"
    base, q = site_params(cb)
    assert base == SITE + "/de/auth/callback"
    assert set(q) == {"code"}                                 # nothing but the one-time code
    for pii in ("Erika", "Muster", "example.com", "li-abc123", "li-access-token"):
        assert pii.lower() not in cb.headers["location"].lower()
    assert '__Host-mauxy_li=""' in cb.headers["set-cookie"] or "max-age=0" in cb.headers["set-cookie"].lower()
    # LinkedIn got the code, our client credentials and the exact redirect_uri
    sent = parse_qs(linkedin["token"].calls.last.request.content.decode())
    assert sent["code"] == ["li-code"] and sent["redirect_uri"] == [REDIRECT_URI]
    assert sent["client_id"] == ["test-client-id"] and sent["grant_type"] == ["authorization_code"]
    assert linkedin["info"].calls.last.request.headers["authorization"] == "Bearer li-access-token-xyz"

    r = exchange(ac, q["code"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "ok" and body["return"] == "/de/kontakt"
    assert body["profile"] == {"sub": "li-abc123", "name": "Erika Muster", "given_name": "Erika",
                               "family_name": "Muster", "email": "erika@example.com", "email_verified": True}
    claims = auth_api.verify_session(body["session"], SITE)
    assert claims["sub"] == "li-abc123" and claims["email"] == "erika@example.com" and claims["typ"] == "session"
    assert claims["email_verified"] is True
    assert auth_api.verify_session(body["session"], "https://www.site.test") is None   # other site
    assert body["expires_at"]


def test_T_AUTH_057_nothing_long_lived_stored(ac, linkedin):
    _, q = site_params(login(ac))
    exchange(ac, q["code"])
    assert rows(ac, "SELECT * FROM auth_code") == []             # profile gone after the exchange
    dump = json.dumps(rows(ac, "SELECT * FROM action_log") + rows(ac, "SELECT * FROM auth_state"))
    for secret in ("Erika", "example.com", "li-abc123", "li-access-token-xyz", "li-code", q["code"]):
        assert secret not in dump
    audit = [r for r in rows(ac, "SELECT * FROM action_log") if r["result"] == "auth_login_ok"]
    assert len(audit) == 1 and audit[0]["email"] == "" and audit[0]["source_ip"] == ""
    assert audit[0]["source_origin"] == SITE


@pytest.mark.parametrize("flag,keep", [
    (True, True), (False, False), ("missing", False), ("true", False), (1, False), (None, False),
], ids=["T-AUTH-058-verified-true", "T-AUTH-089-verified-false", "T-AUTH-090-verified-missing",
        "T-AUTH-091-verified-string", "T-AUTH-092-verified-int", "T-AUTH-093-verified-null"])
def test_email_kept_only_when_verified_is_true(ac, linkedin, flag, keep):
    """email survives only with email_verified exactly True; the flag is also in the signed session."""
    info = {k: v for k, v in PROFILE.items() if k != "email_verified"}
    if flag != "missing":
        info["email_verified"] = flag
    linkedin["info"].mock(return_value=httpx.Response(200, json=info))
    _, q = site_params(login(ac))
    body = exchange(ac, q["code"]).json()
    p, claims = body["profile"], auth_api.verify_session(body["session"], SITE)
    assert p["name"] == "Erika Muster"
    assert p["email"] == ("erika@example.com" if keep else "") and p["email_verified"] is keep
    assert claims["email"] == p["email"] and claims["email_verified"] is keep


def test_T_AUTH_059_tls_verified_no_redirects(ac, linkedin, monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(auth_api.httpx, "AsyncClient", spy)
    login(ac)
    assert seen["verify"] is True and seen["follow_redirects"] is False and seen["timeout"] == auth_api.HTTP_TIMEOUT


# ---------------------------------------------------------------- LinkedIn errors passthrough

@pytest.mark.parametrize("case", CASES["linkedin_errors"], ids=[c["id"] for c in CASES["linkedin_errors"]])
def test_linkedin_error_passthrough(ac, linkedin, case):
    r = start(ac)
    cb = ac.get("/api/auth/linkedin/callback", params={
        "error": case["error"], "error_description": "The+user cancelled <b>x</b>", "state": state_of(r)})
    base, q = site_params(cb)
    assert base == SITE + "/de/auth/callback" and q == {"error": case["expected"]}
    assert not linkedin["token"].called                       # no code exchange on errors


@pytest.mark.parametrize("token_resp,info_resp", [
    (httpx.Response(400, json={"error": "invalid_request"}), None),
    (httpx.Response(200, json={}), None),
    (httpx.Response(200, json={"access_token": "t"}), httpx.Response(401)),
    (httpx.Response(200, json={"access_token": "t"}), httpx.Response(200, json={"name": "no sub"})),
    (httpx.Response(200, json={"access_token": "t"}), httpx.Response(200, text="not json")),
], ids=["T-AUTH-065-token-400", "T-AUTH-066-no-token", "T-AUTH-067-userinfo-401", "T-AUTH-068-no-sub",
        "T-AUTH-069-not-json"])
def test_linkedin_failures_are_login_failed(ac, linkedin, token_resp, info_resp):
    linkedin["token"].mock(return_value=token_resp)
    if info_resp is not None:
        linkedin["info"].mock(return_value=info_resp)
    _, q = site_params(login(ac))
    assert q == {"error": "login_failed"}
    assert rows(ac, "SELECT * FROM auth_code") == []


def test_T_AUTH_070_linkedin_unreachable(ac, linkedin):
    linkedin["token"].mock(side_effect=httpx.ConnectError("boom"))
    _, q = site_params(login(ac))
    assert q == {"error": "login_failed"}


# ---------------------------------------------------------------- state: replay, cookie, expiry

def test_T_AUTH_071_state_replay(ac, linkedin):
    r = start(ac)
    st = state_of(r)
    first = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
    assert "code" in site_params(first)[1]
    ac.cookies.set("__Host-mauxy_li", "irrelevant", domain="mauxy.test")
    second = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
    assert site_params(second)[1] == {"error": "invalid_state"}
    assert linkedin["token"].call_count == 1


def test_T_AUTH_072_missing_cookie(ac, linkedin):
    r = start(ac)
    ac.cookies.clear()
    cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": state_of(r)})
    assert site_params(cb)[1] == {"error": "invalid_state"}
    assert not linkedin["token"].called


def test_T_AUTH_073_mismatched_cookie_login_csrf(ac, linkedin):
    """Attacker starts a login in their browser and sends the victim the callback URL: the
    victim's browser has another (or no) cookie -> refused, the state is burnt."""
    r = start(ac)                                           # attacker's browser
    st, attacker_cookie = state_of(r), ac.cookies.get("__Host-mauxy_li")
    ac.cookies.clear()
    start(ac)                                               # victim's browser: its own cookie
    cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
    assert site_params(cb)[1] == {"error": "invalid_state"}
    assert not linkedin["token"].called
    # burnt: even the attacker's own browser cannot use it any more
    ac.cookies.clear()
    ac.cookies.set("__Host-mauxy_li", attacker_cookie, domain="mauxy.test")
    again = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
    assert site_params(again)[1] == {"error": "invalid_state"}


def test_T_AUTH_074_expired_state(ac, linkedin, monkeypatch):
    r = start(ac)
    monkeypatch.setattr(auth_api, "STATE_TTL", -1)          # every signature is now "too old"
    cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": state_of(r)})
    assert site_params(cb) == (SITE + "/de/auth/callback", {"error": "invalid_state"})
    assert not linkedin["token"].called


def test_T_AUTH_075_forged_or_tampered_state(ac, linkedin):
    from itsdangerous import URLSafeTimedSerializer
    r = start(ac)
    (row,) = rows(ac, "SELECT id FROM auth_state")
    forged = URLSafeTimedSerializer("other-key-other-key-other-key-0000", salt="mauxy-auth-state").dumps(
        {"i": row["id"], "o": SITE, "l": "de", "r": "/de/"})
    for st in (forged, state_of(r)[:-3] + "AAA", "", "x" * 50):
        cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
        assert site_params(cb) == (SITE + "/de/auth/callback", {"error": "invalid_state"})
    # a correctly signed state with a foreign origin / return path (defence in depth)
    for bad in ({"o": "https://evil.example"}, {"r": "//evil.example"}, {"l": "fr"}):
        st = auth_api._state_signer.dumps({"i": row["id"], "o": SITE, "l": "de", "r": "/de/", **bad})
        cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": st})
        assert site_params(cb) == (SITE + "/de/auth/callback", {"error": "invalid_state"})
    assert not linkedin["token"].called


def test_T_AUTH_076_session_token_is_no_state(ac, linkedin):
    """Own salts: a session token never passes as state (and vice versa)."""
    _, q = site_params(login(ac))
    session = exchange(ac, q["code"]).json()["session"]
    cb = ac.get("/api/auth/linkedin/callback", params={"code": "li-code", "state": session})
    assert site_params(cb)[1] == {"error": "invalid_state"}
    assert auth_api.verify_session(state_of(start(ac))) is None


# ---------------------------------------------------------------- one-time code exchange

def test_T_AUTH_077_code_replay(ac, linkedin):
    _, q = site_params(login(ac))
    assert exchange(ac, q["code"]).status_code == 200
    r = exchange(ac, q["code"])
    assert r.status_code == 400 and r.json() == {"status": "invalid_code"}


def test_T_AUTH_078_code_binding_mismatch_burns_code(ac, linkedin):
    """A code injected into another tab / browser (no matching binding) is useless and burnt."""
    _, q = site_params(login(ac))
    r = exchange(ac, q["code"], binding="z" * 43)
    assert r.status_code == 400 and r.json() == {"status": "invalid_code"}
    assert exchange(ac, q["code"]).status_code == 400


@pytest.mark.parametrize("origin", [None, "https://evil.example", "https://www.site.test", "https://site.test.evil.example"],
                         ids=["T-AUTH-079-no-origin", "T-AUTH-080-foreign", "T-AUTH-081-other-site", "T-AUTH-082-lookalike"])
def test_code_origin_mismatch(ac, linkedin, origin):
    _, q = site_params(login(ac))
    r = exchange(ac, q["code"], origin=origin)
    assert r.status_code == 400 and r.json() == {"status": "invalid_code"}


def test_T_AUTH_083_code_expires_after_60s(ac, linkedin, monkeypatch):
    from datetime import timedelta
    _, q = site_params(login(ac))
    later = auth_api._now() + timedelta(seconds=auth_api.CODE_TTL + 1)
    monkeypatch.setattr(auth_api, "_now", lambda: later)
    r = exchange(ac, q["code"])
    assert r.status_code == 400 and r.json() == {"status": "invalid_code"}
    assert rows(ac, "SELECT * FROM auth_code") == []             # purged


def test_T_AUTH_084_unknown_code_and_bad_payload(ac):
    assert exchange(ac, "u" * 43).json() == {"status": "invalid_code"}
    assert ac.post("/api/auth/session", json={"code": "short", "binding": BINDING},
                   headers={"Origin": SITE}).status_code == 422
    assert ac.post("/api/auth/session", json={"code": "u" * 43, "binding": BINDING, "extra": 1},
                   headers={"Origin": SITE}).status_code == 422


def test_T_AUTH_085_session_expiry(ac, linkedin, monkeypatch):
    _, q = site_params(login(ac))
    token = exchange(ac, q["code"]).json()["session"]
    monkeypatch.setattr(auth_api, "AUTH_SESSION_TTL", -1)
    assert auth_api.verify_session(token) is None


def test_T_AUTH_086_cors_preflight_for_session(ac):
    r = ac.options("/api/auth/session", headers={
        "Origin": SITE, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == SITE
    r = ac.options("/api/auth/session", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"
    assert "access-control-allow-credentials" not in r.headers


def test_T_AUTH_087_health_detail_reports_login_config(ac, monkeypatch):
    import main
    monkeypatch.setattr(main, "_sink_health", {"checked_at": -1e9, "detail": {}})  # keep the 30 s cache clean
    body = ac.get("/health/detail").json()
    assert body["auth_linkedin"] == "configured"
    assert "test-client" not in json.dumps(body)


def test_T_AUTH_088_purge_drops_old_rows(ac, linkedin, monkeypatch):
    from datetime import timedelta
    start(ac)
    login(ac)
    later = auth_api._now() + timedelta(seconds=auth_api.STATE_TTL + 1)
    monkeypatch.setattr(auth_api, "_now", lambda: later)
    ac.portal.call(auth_api.purge, ac.app.state.db)
    assert rows(ac, "SELECT * FROM auth_state") == [] and rows(ac, "SELECT * FROM auth_code") == []

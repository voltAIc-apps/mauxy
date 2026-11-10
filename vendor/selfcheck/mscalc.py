"""
Microsoft -> Odoo 5-year comparison (SPEC-022), Python side.

Single source of rules: website/src/data/mscalc.json. Twin of website/src/utils/mscalc.ts; both must
return identical results (parity cases tests/cases/mscalc_cases.json, T-MSC-0xx). Owner decision
2026-11-10 (D-065): the page shows no computed prices; the figures come in with the embedded
Selbst-Check and mauxy (vendored copy via scripts/sync_selfcheck.py) computes after the double opt-in:
compare_selfcheck -> mail_block (customer, gated by mail_figures_allowed) + operator_line (English).
Stdlib only.

Model (EUR, net), see the TS file header for the full description:
  stay recurring/year  = ERP licence + partner/year + office seats x M365 rate x 12
  move recurring/year  = users x (Odoo licence + support) x 12 + hosting x 12 + seats x OnlyOffice x 12
  migration one-off    = days x day rate (low/high = mid x range %), rounded to round_one_off_to
  break-even month     = first month where cumulative move <= cumulative stay, else None
Float operations run in the same order as in TS (iterative growth, no pow) so both agree.

Rule file resolution (load_rules without a path):
  1. env MSCALC_RULES_FILE
  2. mscalc.json next to this file (vendored copy)
  3. <repo>/website/src/data/mscalc.json
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

Values = dict[str, Any]


def default_rules_path() -> Path:
    env = os.environ.get("MSCALC_RULES_FILE")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    sibling = here / "mscalc.json"
    if sibling.is_file():
        return sibling
    # backend/libs/mscalc.py -> repo root is two levels up
    return here.parent.parent / "website" / "src" / "data" / "mscalc.json"


def load_rules(path: str | Path | None = None) -> dict:
    """Read the rules JSON (utf-8)."""
    with open(path or default_rules_path(), encoding="utf-8") as fh:
        return json.load(fh)


def js_round(x: float) -> int:
    """Math.round of JavaScript (half up) so Python and TS agree on .5 cases."""
    return int(math.floor(x + 0.5))


def _is_num(v: Any) -> bool:
    """Finite int/float, never bool (JS typeof 'number' + isFinite)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean_inputs(rules: dict, raw: Values | None) -> dict[str, float]:
    """Visitor inputs: not a number or <= 0 -> 0; above the limit -> limit; int kinds floored.
    Missing keys take the rules default."""
    out: dict[str, float] = {}
    for d in rules["inputs"]:
        v = raw[d["id"]] if raw and d["id"] in raw else d["default"]
        n = v if _is_num(v) and v > 0 else 0
        mx = rules["limits"][d["limit"]]
        if n > mx:
            n = mx
        if d["kind"] == "int":
            n = math.floor(n)
        out[d["id"]] = n
    return out


def resolve_rates(rules: dict, overrides: Values | None) -> dict[str, float]:
    """Assumption values with overrides; an override outside min..max or not a number is ignored."""
    out: dict[str, float] = {}
    for a in rules["assumptions"]:
        v = overrides.get(a["id"]) if overrides else None
        out[a["id"]] = v if _is_num(v) and a["min"] <= v <= a["max"] else a["value"]
    return out


def unconfirmed(rules: dict) -> list[str]:
    """Assumptions still waiting for the owner (release gate, SPEC-022 s.7)."""
    return [a["id"] for a in rules["assumptions"] if a["status"] == "owner_to_confirm"]


def compute(rules: dict, raw: Values | None = None, overrides: Values | None = None) -> dict | None:
    """Full comparison as plain data (parity with mscalc.ts compute). None = nothing to compare."""
    inp = clean_inputs(rules, raw)
    rate = resolve_rates(rules, overrides)
    users, seats = inp["users"], inp["office_seats"]
    if users < 1 and seats < 1:
        return None

    # --- recurring cost per year (year 1)
    ms_licence = 0
    if users >= 1:
        ms_licence = (inp["ms_licence_total_year"] if inp["ms_licence_total_year"] > 0
                      else users * rate["ms_erp_licence_user_month"] * 12)
    stay_annual = ms_licence + inp["partner_year"] + seats * rate["ms_office_seat_month"] * 12

    move_annual = seats * rate["onlyoffice_seat_month"] * 12
    if users >= 1:
        move_annual = (users * (rate["odoo_licence_user_month"] + rate["odoo_support_user_month"]) * 12
                       + rate["odoo_hosting_month"] * 12 + move_annual)

    # --- migration one-off
    days = 0
    if users >= 1:
        days = (rate["base_days"] + inp["customisations"] * rate["days_per_customisation"]
                + inp["history_years"] * rate["days_per_history_year"]
                + math.ceil(users / 10) * rate["days_per_10_users"])
    if seats >= 1:
        days = days + rate["office_base_days"] + seats * rate["office_days_per_seat"]
    step = rules["round_one_off_to"]
    mid = days * rate["day_rate"]
    one_off = {
        "low": js_round(mid * rate["range_low_pct"] / 100 / step) * step,
        "mid": js_round(mid / step) * step,
        "high": js_round(mid * rate["range_high_pct"] / 100 / step) * step,
    }

    # --- years and months
    years = rules["years"]
    g_stay = 1 + rate["ms_increase_pct"] / 100
    g_move = 1 + rate["odoo_increase_pct"] / 100
    stay_year: list[float] = []
    move_year: list[float] = []
    fs = fm = 1
    for _ in range(years):
        stay_year.append(stay_annual * fs)
        move_year.append(move_annual * fm)
        fs = fs * g_stay
        fm = fm * g_move
    stay_total = inp["ms_one_off"]
    move_rec = 0
    cumulative = []
    for y in range(years):
        stay_total = stay_total + stay_year[y]
        move_rec = move_rec + move_year[y]
        cumulative.append({"year": y + 1, "stay": js_round(stay_total), "move": js_round(one_off["mid"] + move_rec)})

    def break_even(one_off_value: float) -> int | None:
        s = inp["ms_one_off"]
        m = one_off_value
        for month in range(1, years * 12 + 1):
            y = (month - 1) // 12
            s = s + stay_year[y] / 12
            m = m + move_year[y] / 12
            if m <= s:
                return month
        return None

    move_total = {k: js_round(one_off[k] + move_rec) for k in ("low", "mid", "high")}
    stay_rounded = js_round(stay_total)
    return {
        "years": years,
        "inputs": inp,
        "rates": rate,
        "stay": {"annual": js_round(stay_annual), "total": stay_rounded},
        "move": {"annual": js_round(move_annual), "one_off": one_off, "days": js_round(days * 10) / 10,
                 "total": move_total},
        # low savings = high migration cost and vice versa
        "savings": {"low": stay_rounded - move_total["high"], "mid": stay_rounded - move_total["mid"],
                    "high": stay_rounded - move_total["low"]},
        "break_even": {k: break_even(one_off[k]) for k in ("low", "mid", "high")},
        "cumulative": cumulative,
        "unconfirmed": unconfirmed(rules),
    }


def break_even_label(month: int | None, years: int, lang: str = "de") -> str:
    """'Monat 14 (Jahr 2)' / 'month 14 (year 2)'; None -> not within the horizon."""
    if month is None:
        return f"nicht innerhalb von {years} Jahren" if lang == "de" else f"not within {years} years"
    year = (month - 1) // 12 + 1
    return f"Monat {month} (Jahr {year})" if lang == "de" else f"month {month} (year {year})"


def format_eur(n: float, lang: str = "de") -> str:
    """Whole euros like the page (de-DE '12.345 €', en-GB '€12,345')."""
    v = js_round(n)
    s = f"{abs(v):,}"
    sign = "-" if v < 0 else ""
    if lang == "de":
        return f"{sign}{s.replace(',', '.')} €"
    return f"{sign}€{s}"


# --------------------------------------------------------------------------- Selbst-Check + mail
# Owner decision 2026-11-10 (D-065): the page computes nothing. The visitor's figures come in with the
# Selbst-Check (stay stage, selfcheck.json); mauxy (vendored copy of this file) computes after the double
# opt-in and mails the comparison. Customer figures are gated (mail_figures_allowed).


def applies(rules: dict, answers: dict | None, stay: dict | None) -> bool:
    """Comparison only for a Microsoft system (answer 'system' has the rules' system_option) with stay figures."""
    system = (answers or {}).get("system")
    return isinstance(system, list) and rules["selfcheck"]["system_option"] in system and bool(stay)


def from_stay(rules: dict, stay: dict | None) -> Values:
    """Selbst-Check stay fields -> calculator inputs (rules selfcheck.fields). Missing field: 'zero' -> 0,
    'default' -> the input's default (cautious estimate). Values themselves are cleaned by clean_inputs."""
    defaults = {d["id"]: d["default"] for d in rules["inputs"]}
    raw: Values = {}
    for field, m in rules["selfcheck"]["fields"].items():
        v = (stay or {}).get(field)
        if v is None:
            v = defaults[m["input"]] if m["missing"] == "default" else 0
        raw[m["input"]] = v
    return raw


def mail_figures_allowed(rules: dict) -> bool:
    """Gate of the customer mail (owner 2026-11-10): figures only when the owner switched them on
    (customer_mail_figures true) AND no rate is owner_to_confirm. Otherwise the neutral sentence."""
    return rules.get("customer_mail_figures") is True and not unconfirmed(rules)


def compare_selfcheck(rules: dict, answers: dict | None, stay: dict | None) -> dict | None:
    """Comparison for a confirmed Selbst-Check, or None (not Microsoft / no figures / nothing to compare)."""
    if not applies(rules, answers, stay):
        return None
    return compute(rules, from_stay(rules, stay))


def _days(n: float, lang: str) -> str:
    s = str(int(n)) if float(n).is_integer() else str(n)
    return s.replace(".", ",") if lang == "de" else s


def _values(r: dict, lang: str) -> dict[str, str]:
    """Template values of the mail texts, formatted for lang."""
    eur = lambda n: format_eur(n, lang)  # noqa: E731
    be = {k: break_even_label(r["break_even"][k], r["years"], lang) for k in ("low", "mid", "high")}
    return {
        "years": str(r["years"]),
        "stay_total": eur(r["stay"]["total"]), "stay_annual": eur(r["stay"]["annual"]),
        "move_low": eur(r["move"]["total"]["low"]), "move_mid": eur(r["move"]["total"]["mid"]),
        "move_high": eur(r["move"]["total"]["high"]), "move_annual": eur(r["move"]["annual"]),
        "one_off_low": eur(r["move"]["one_off"]["low"]), "one_off_high": eur(r["move"]["one_off"]["high"]),
        "days": _days(r["move"]["days"], lang),
        "break_even_low": be["low"], "break_even_mid": be["mid"], "break_even_high": be["high"],
    }


def mail_block(rules: dict, r: dict, lang: str = "de") -> tuple[str, list[str]]:
    """(title, lines) of the customer result mail. Gate closed -> only the neutral sentence, no figure."""
    lang = lang if lang in ("de", "en") else "de"
    m = rules["mail"]
    title = m["title"][lang].format(years=r["years"])
    if not mail_figures_allowed(rules):
        return title, [m["pending"][lang].format(years=r["years"])]
    v = _values(r, lang)
    ln = m["lines"]
    keys = ["stay", "move", "one_off", "break_even" if r["break_even"]["mid"] is not None else "break_even_none", "note"]
    return title, [ln[k][lang].format(**v) for k in keys]


def operator_line(rules: dict, r: dict) -> str:
    """One English summary line for the operator mail and the CRM lead; figures always, flagged while
    any rate is unconfirmed; says whether the customer mail carried them."""
    v = _values(r, "en")
    ids = unconfirmed(rules)
    flag = rules["mail"]["unconfirmed_flag"].format(n=len(ids)) if ids else ""
    customer = "figures" if mail_figures_allowed(rules) else "neutral sentence (gate closed)"
    return rules["mail"]["operator"].format(flag=flag, customer=customer, **v)

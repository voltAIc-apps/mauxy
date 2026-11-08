"""
Selbst-Check rules engine (SPEC-020), Python side.

Single source of rules: website/src/data/selfcheck.json (same pattern as shop.json /
shop_data.py). The page evaluates the same file in website/src/utils/selfcheck.ts; both must
return identical results (parity cases tests/cases/selfcheck_cases.json, T-SC-0xx).

Stdlib only on purpose: scripts/sync_selfcheck.py vendors this file plus the JSON into mauxy
(public DOI service), which renders the result mail from validated answers with it.

Rule file resolution (load_rules without a path):
  1. env SELFCHECK_RULES_FILE
  2. selfcheck.json next to this file (vendored copy in mauxy)
  3. <repo>/website/src/data/selfcheck.json (flywheel checkout)
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

Answers = dict[str, Any]
LANGS = ("de", "en")
PICKS = ("mail", "call", "radar")


# --------------------------------------------------------------------------- loading


def default_rules_path() -> Path:
    env = os.environ.get("SELFCHECK_RULES_FILE")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    sibling = here / "selfcheck.json"
    if sibling.is_file():
        return sibling
    # backend/libs/selfcheck.py -> repo root is two levels up
    return here.parent.parent / "website" / "src" / "data" / "selfcheck.json"


def load_rules(path: str | Path | None = None) -> dict:
    """Read the rules JSON (utf-8)."""
    with open(path or default_rules_path(), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- helpers


def js_round(x: float) -> int:
    """Math.round of JavaScript (half up), so Python and TS agree on .5 cases."""
    return int(math.floor(x + 0.5))


def _num(x: float) -> float | int:
    """JSON number like JS would print it: 12.0 -> 12."""
    return int(x) if float(x).is_integer() else x


def matches(cond: dict | None, answers: Answers) -> bool:
    """Evaluate one condition of the rules DSL. None = always true.

    {q, eq} single answer equals; {q, in} single answer in list; {q, has} multi contains;
    {q, count_gte} multi length >= n; {q, only} multi is exactly [x]; {any:[...]}, {all:[...]}.
    An unanswered question never matches (so partial answers default to 'good').
    """
    if cond is None:
        return True
    if "any" in cond:
        return any(matches(c, answers) for c in cond["any"])
    if "all" in cond:
        return all(matches(c, answers) for c in cond["all"])
    v = answers.get(cond["q"])
    if "eq" in cond:
        return isinstance(v, str) and v == cond["eq"]
    if "in" in cond:
        return isinstance(v, str) and v in cond["in"]
    lst = v if isinstance(v, list) else []
    if "has" in cond:
        return cond["has"] in lst
    if "count_gte" in cond:
        return len(lst) >= cond["count_gte"]
    if "only" in cond:
        return len(lst) == 1 and lst[0] == cond["only"]
    raise ValueError(f"unknown condition {cond!r}")


def resolve_question(q: dict, answers: Answers) -> dict:
    """Question with its first matching variant applied (text + option labels)."""
    for var in q.get("variants") or []:
        if matches(var["when"], answers):
            opts = [
                {**o, "label": var.get("options", {}).get(o["id"], o["label"])}
                for o in q["options"]
            ]
            return {**q, "text": var.get("text", q["text"]), "options": opts, "variant": True}
    return {**q, "variant": False}


def visible_questions(rules: dict, answers: Answers) -> list[dict]:
    """Questions shown for these answers, in order, variants resolved."""
    return [
        resolve_question(q, answers)
        for q in rules["questions"]
        if matches(q.get("show_if"), answers)
    ]


def is_answered(q: dict, answers: Answers) -> bool:
    v = answers.get(q["id"])
    if q.get("multi"):
        return isinstance(v, list) and len(v) > 0
    return isinstance(v, str) and v != ""


def precision(rules: dict, answered: int, total: int) -> dict:
    """Richtangebot precision: level word + width/left of the narrowing band (percent)."""
    p = rules["precision"]
    ratio = answered / total if total else 0.0
    level = "precise" if ratio >= p["precise_min"] else ("solid" if ratio >= p["solid_min"] else "rough")
    width = js_round(p["band_max"] - ratio * p["band_shrink"])
    return {"level": level, "width": width, "left": _num(50 - width / 2)}


def progress(rules: dict, answers: Answers) -> dict:
    qs = visible_questions(rules, answers)
    total = len(qs)
    answered = sum(1 for q in qs if is_answered(q, answers))
    first_open = next((i for i, q in enumerate(qs) if not is_answered(q, answers)), -1)
    remaining = total - answered
    return {
        "total": total,
        "answered": answered,
        "remaining": remaining,
        "minutes": max(1, js_round(remaining * rules["minutes_per_question"])),
        "resume_index": total - 1 if first_open < 0 else first_open,
        "complete": remaining == 0,
        "precision": precision(rules, answered, total),
    }


# --------------------------------------------------------------------------- evaluation


def evaluate(rules: dict, answers: Answers, lang: str = "de") -> dict:
    """Traffic lights, top risks, waves, headline and chips for these answers."""
    lang = lang if lang in LANGS else "de"
    levels = rules["levels"]

    dims = []
    for d in rules["dimensions"]:
        level = "bad" if matches(d["bad"], answers) else ("mid" if matches(d["mid"], answers) else "good")
        dims.append({
            "id": d["id"],
            "level": level,
            "name": d["name"][lang],
            "label": levels[level]["label"][lang],
            "color": levels[level]["color"],
            "note": d["notes"][level][lang],
        })

    pool = [r for r in rules["risks"]["pool"] if matches(r.get("when"), answers)]
    risks = [
        {"n": i + 1, "id": r["id"], "title": r["title"][lang], "rule": r["rule"][lang], "article": r.get("article")}
        for i, r in enumerate(pool[: rules["risks"]["max"]])
    ]

    w = rules["waves"]
    areas = answers.get("areas") if isinstance(answers.get("areas"), list) else []
    waves = [{"n": 1, "id": "first", "title": w["first"]["title"][lang], "items": w["first"]["items"][lang], "areas": []}]
    for nxt in w["next"]:
        hit = [a for a in nxt["areas"] if a in areas]
        if hit:
            waves.append({
                "n": len(waves) + 1,
                "id": nxt["id"],
                "title": nxt["title"][lang],
                "items": ", ".join(w["area_names"][a][lang] for a in hit),
                "areas": hit,
            })

    bad = sum(1 for d in dims if d["level"] == "bad")
    headline = next(h for h in rules["headlines"] if bad >= h["min_bad"])

    ch = rules["chips"]
    system = answers.get("system") if isinstance(answers.get("system"), list) else []
    sys_names = [ch["system_names"][k][lang] for k in system if k in ch["system_names"]]
    chips = [" + ".join(sys_names) or ch["system_none"][lang]]
    users = answers.get("users")
    if isinstance(users, str) and users in ch["users"]:
        chips.append(ch["users"][users][lang])
    chips.append(ch["areas"][lang].replace("{n}", str(len(areas))))

    return {
        "dims": dims,
        "risks": risks,
        "waves": waves,
        "bad_count": bad,
        "headline": headline["text"][lang],
        "chips": chips,
    }


def stay_cost(rules: dict, inputs: dict | None) -> dict | None:
    """Stage 2 'Was kostet es, zu bleiben?': only the reader's own figures (EUR).

    cost = years * (licence + support) + offer; per_user_year = (licence + support) / users.
    Returns None when licence and support are both missing/zero.
    """
    if not inputs:
        return None
    years = rules["stay_cost"]["years"]

    def val(key: str) -> float:
        v = inputs.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0

    licence, support, offer, users = val("licence"), val("support"), val("offer"), val("users")
    annual = licence + support
    if annual <= 0:
        return None
    cost = years * annual + offer
    return {
        "years": years,
        "annual": _num(annual),
        "cost": _num(cost),
        "per_user_year": js_round(annual / users) if users >= 1 else None,
    }


def snapshot(rules: dict, answers: Answers, lang: str = "de", stay: dict | None = None) -> dict:
    """Everything the page shows, as plain data. Compared 1:1 with the TS snapshot (parity)."""
    qs = visible_questions(rules, answers)
    return {
        "visible": [q["id"] for q in qs],
        "texts": {q["id"]: q["text"][lang] for q in qs if q.get("variant")},
        "progress": progress(rules, answers),
        "evaluation": evaluate(rules, answers, lang),
        "stay_cost": stay_cost(rules, stay),
    }


# --------------------------------------------------------------------------- validation


def validate_answers(rules: dict, answers: Any) -> tuple[Answers, list[str]]:
    """Check browser-supplied answers against the rules. Returns (clean, errors).

    Errors: unknown question id, wrong type, unknown option, duplicates, exclusive option
    combined with others. Answers to questions hidden by show_if are dropped (the page keeps
    them in storage when the reader changes Q1), not rejected. Partial answers are valid.
    """
    if not isinstance(answers, dict):
        return {}, ["answers: not an object"]
    by_id = {q["id"]: q for q in rules["questions"]}
    errors: list[str] = []
    for key in answers:
        if key not in by_id:
            errors.append(f"{key}: unknown question")
    clean: Answers = {}
    for qid, q in by_id.items():
        if qid not in answers:
            continue
        v = answers[qid]
        opt_ids = {o["id"] for o in q["options"]}
        if q.get("multi"):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errors.append(f"{qid}: expected a list of option ids")
                continue
            if len(set(v)) != len(v):
                errors.append(f"{qid}: duplicate option")
            bad = [x for x in v if x not in opt_ids]
            if bad:
                errors.append(f"{qid}: unknown option {bad}")
            excl = {o["id"] for o in q["options"] if o.get("exclusive")}
            if len(v) > 1 and any(x in excl for x in v):
                errors.append(f"{qid}: exclusive option combined with others")
            if v:
                clean[qid] = list(v)
        else:
            if not isinstance(v, str):
                errors.append(f"{qid}: expected an option id")
                continue
            if v not in opt_ids:
                errors.append(f"{qid}: unknown option {v!r}")
                continue
            clean[qid] = v
    if errors:
        return {}, errors
    visible = {q["id"] for q in visible_questions(rules, clean)}
    return {k: v for k, v in clean.items() if k in visible}, []


# --------------------------------------------------------------------------- mail text


def format_eur(amount: float, lang: str = "de") -> str:
    """de: 12.345 €, en: €12,345 (whole euros; the inputs are estimates)."""
    n = f"{js_round(amount):,}"
    return f"{n.replace(',', '.')} €" if lang == "de" else f"€{n}"


def answer_lines(rules: dict, answers: Answers, lang: str = "de") -> list[str]:
    """'Question: label, label' per visible question (unanswered: '-')."""
    out = []
    for q in visible_questions(rules, answers):
        labels = {o["id"]: o["label"][lang] for o in q["options"]}
        v = answers.get(q["id"])
        vals = v if isinstance(v, list) else ([v] if isinstance(v, str) else [])
        out.append(f"{q['text'][lang]} {', '.join(labels[x] for x in vals if x in labels) or '-'}")
    return out


def result_text(rules: dict, answers: Answers, lang: str = "de", stay: dict | None = None) -> str:
    """Plain-text body block of the result mail (no greeting / signature)."""
    lang = lang if lang in LANGS else "de"
    lab = rules["mail"]["result"]["labels"]
    base = f"{rules['website']}/{lang}"
    prog = progress(rules, answers)
    lines: list[str] = []
    if prog["complete"]:
        ev = evaluate(rules, answers, lang)
        lines += [ev["headline"], " | ".join(ev["chips"]), "", lab["lights"][lang]]
        lines += [f"- {d['name']}: {d['label']} ({d['note']})" for d in ev["dims"]]
        lines += ["", lab["risks"][lang]]
        for r in ev["risks"]:
            lines.append(f"{r['n']}. {r['title']}: {r['rule']}")
            if r["article"]:
                lines.append(f"   {lab['read'][lang]}: {base}/wissen/{r['article']}")
        lines += ["", lab["waves"][lang]]
        lines += [f"{lab['wave'][lang]} {w['n']}: {w['title']} ({w['items']})" for w in ev["waves"]]
    sc = stay_cost(rules, stay)
    if sc:
        lines += ["", lab["stay"][lang], (
            f"{sc['years']} Jahre bleiben: {format_eur(sc['cost'], lang)}. Ein Wechsel rechnet sich, "
            f"wenn er weniger als {format_eur(sc['cost'], lang)} kostet."
            if lang == "de" else
            f"Staying {sc['years']} years: {format_eur(sc['cost'], lang)}. A switch pays off if it "
            f"costs less than {format_eur(sc['cost'], lang)}."
        )]
    lines += ["", lab["answers"][lang]]
    lines += [f"- {line}" for line in answer_lines(rules, answers, lang)]
    return "\n".join(lines).strip()


def pick_titles(rules: dict, picks: list[str], lang: str = "de") -> list[str]:
    titles = {p["id"]: p["title"][lang] for p in rules["picks"]}
    return [titles[p] for p in PICKS if p in picks]

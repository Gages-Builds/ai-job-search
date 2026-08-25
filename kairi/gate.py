#!/usr/bin/env python3
"""Search-law gate: enforce the operator's hard job-search criteria in code.

Run:  python kairi/gate.py --input freehire-results.json
      curl -s ... | python kairi/gate.py --out /somewhere/run.json

Why this exists: every threshold this framework applies to job postings used
to live in prose documents that a language model was trusted to obey. A
threshold whose only reader is a prompt is choreography, not enforcement -
gates are code or they are theater. This program reads the operator's ratified
rules (the "search law") as data and applies them pass/fail, in a fixed order,
BEFORE anything else scores a posting. It never invents thresholds of its own.

The law (see kairi/README.md and kairi/search-law.example.json):
  1. Territory gate   - the role must be remote or sit in a listed city.
                        A listed city is read from the structured 'cities'
                        facet first, then from the free-text 'location' the
                        posting itself declares (on token boundaries, never
                        fuzzy). Unresolved geography is UNKNOWN, not a
                        mismatch: a gap in the data is not evidence against
                        a posting.
  2. Absolute floor   - a STATED salary whose top is below the absolute floor
                        is rejected. Most postings state no salary at all;
                        that is UNKNOWN and passes to review - dropping
                        unpriced postings would invert the operator's posture.
  3. Soft-floor check - postings that cleared both gates are compared with the
                        applicable soft floor (remote or their group's). A
                        shortfall is a FLAG carrying the amount, never a
                        rejection: the operator applies broadly and declines
                        later.

Safety properties, enforced rather than promised:
  - The law file must exist and parse; otherwise the gate exits non-zero
    instead of running with guessed rules. Defaults would be invented law.
  - Posting text is untrusted data. Only declared fields are read; nothing in
    a title or body can steer, execute, or override a gate. The description
    body is never even loaded into memory.
  - Nothing is fabricated. A field the posting does not state is recorded as
    null/"unknown"; a foreign-currency figure is never converted.
  - The run artifact is written OUTSIDE this repository by default, because a
    public repository is the wrong place for someone's search criteria or
    search history; an explicit --out inside the repo is refused.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# The real law lives outside the repository (a public repo must not hold the
# operator's cities and salary floors); see kairi/README.md.
DEFAULT_LAW_PATH = Path.home() / ".local" / "share" / "kairi" / "search-law.json"
DEFAULT_OUT_DIR = Path.home() / ".local" / "share" / "kairi" / "runs"
README_HINT = "kairi/README.md"

# Run artifacts carry personal search data, so they must never land inside
# this (public) repository. Resolved from this file's location: kairi/gate.py
# sits one level below the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"
VERDICT_NOT_REACHED = "not_reached"


class GateError(Exception):
    """A condition the gate refuses to proceed past (bad law, bad input, unsafe output path)."""


# ---------------------------------------------------------------------------
# The law file
# ---------------------------------------------------------------------------

def _norm_city(name):
    """Normalize a city name for comparison: collapse whitespace, ignore case.

    Whitespace-tolerant so "New York" matches "new   york"; case folded so
    "OSLO" matches "Oslo". No fuzzy matching beyond that - a city either is
    listed or it is not.
    """
    return " ".join(str(name).split()).casefold()


def _require_number(container, key, where):
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError(f"{where}: '{key}' must be a number (got {value!r})")
    return value


def load_law(path):
    """Read and validate the search-law file. Fail closed: any problem is fatal.

    A gate that falls back to defaults when the real law is missing would be
    inventing its own thresholds - worse than no gate - so absence, bad JSON,
    or a wrong-shaped file all stop the run here, naming the path.
    """
    path = Path(path).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(
            f"search-law file not readable: {path} ({exc}).\n"
            f"The gate refuses to run without the real law - never with guessed "
            f"defaults. Copy kairi/search-law.example.json there and fill it in: "
            f"see {README_HINT}."
        )
    try:
        law = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError(
            f"search-law file is not valid JSON: {path} (line {exc.lineno}). "
            f"Fix the file - see {README_HINT}."
        )

    where = f"search-law file {path}"
    if not isinstance(law, dict):
        raise GateError(f"{where}: top-level JSON value must be an object")

    for key in ("version", "ruled"):
        if not isinstance(law.get(key), str) or not law.get(key):
            raise GateError(f"{where}: '{key}' must be a non-empty string")
    currency = law.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise GateError(f"{where}: 'currency' must be a non-empty string")
    _require_number(law, "absolute_floor", where)
    if not isinstance(law.get("posture"), str) or not law.get("posture"):
        raise GateError(f"{where}: 'posture' must be a non-empty string describing how borderline roles should be treated")

    groups = law.get("groups")
    if not isinstance(groups, list):
        raise GateError(f"{where}: 'groups' must be a list")
    for index, group in enumerate(groups):
        gwhere = f"{where}: groups[{index}]"
        if not isinstance(group, dict):
            raise GateError(f"{gwhere} must be an object")
        if not isinstance(group.get("name"), str) or not group.get("name"):
            raise GateError(f"{gwhere}: 'name' must be a non-empty string")
        cities = group.get("cities")
        if not isinstance(cities, list) or not all(isinstance(c, str) and c.strip() for c in cities):
            raise GateError(f"{gwhere}: 'cities' must be a list of non-empty strings")
        _require_number(group, "soft_floor", gwhere)

    remote = law.get("remote")
    if not isinstance(remote, dict):
        raise GateError(f"{where}: 'remote' must be an object")
    _require_number(remote, "soft_floor", f"{where}: remote")
    if not isinstance(remote.get("precedence_over_groups"), bool):
        raise GateError(f"{where}: remote.precedence_over_groups must be true or false")

    return law


def group_cities_by_norm(law):
    """Map normalized city name -> {'group': name, 'city': spelling in the law}.

    The territory lookup for BOTH match paths (the structured 'cities' facet
    and the free-text 'location' string). The original spelling is kept so a
    record can name the law's city, not just its group.
    """
    lookup = {}
    for group in law["groups"]:
        for city in group["cities"]:
            lookup.setdefault(_norm_city(city), {"group": group["name"], "city": city})
    return lookup


# ---------------------------------------------------------------------------
# The three gates, always evaluated in this order, before any scoring
# ---------------------------------------------------------------------------

def _stated_salary_bounds(posting):
    """((min, max) as stated, ignoring absent/non-numeric values). Never defaults."""
    def stated(key):
        value = posting.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    return stated("salary_min"), stated("salary_max")


# Separators for free-text location strings: every run of non-alphanumeric
# characters ("|", "-", ",", "/", whitespace, ...) splits a token. Underscore
# counts as a separator too - it is punctuation to a reader, not a letter.
_LOCATION_TOKEN_SEPARATOR = re.compile(r"[\W_]+", re.UNICODE)


def _match_in_location(location_text, city_lookup):
    """Find a law city inside a free-text 'location' string, on token boundaries.

    The location is split into word tokens; each law city (normalized) must
    appear as CONSECUTIVE tokens, so "Fort Myers" matches inside
    "US - Florida - Fort Myers" while "Austin" does not match inside
    "Austintown" and "Provo" does not match inside "Provost Road". This reads
    a field the posting itself declared - it is never inference, and never
    fuzzy: either the listed name is there as its own words or it is not.

    Returns {'city': spelling from the law, 'group': group name} or None.
    """
    tokens = [token for token in _LOCATION_TOKEN_SEPARATOR.split(location_text) if token]
    if not tokens:
        return None
    for norm_city, entry in city_lookup.items():
        width = len(norm_city.split())
        if not width or width > len(tokens):
            continue
        for start in range(len(tokens) - width + 1):
            if _norm_city(" ".join(tokens[start:start + width])) == norm_city:
                return entry
    return None


def territory_gate(posting, city_lookup):
    """Gate 1: is the role somewhere the operator would work?

    PASS  - remote, or the posting resolves to at least one listed city.
            Resolution tries the structured 'cities' facet first, then the
            free-text 'location' string the posting declares; whichever field
            produced the match is recorded in 'matched_via' ('cities' or
            'location'). A remote posting's group match is recorded too -
            even though remote passes regardless - because when
            remote.precedence_over_groups is false the group's soft floor,
            not the remote one, must be applied later.
    FAIL  - geography IS stated (a city list, a non-blank location, or both)
            and matches no listed city, and the role is not remote.
    UNKNOWN - nothing resolves the geography at all: 'cities' empty AND
              'location' blank AND 'work_mode' null. A data gap is never
              counted as a mismatch; tallied in unresolved_geography.
    """
    work_mode = posting.get("work_mode")
    cities = posting.get("cities") or []
    if not isinstance(cities, list):
        cities = []
    location = posting.get("location")
    location_text = location.strip() if isinstance(location, str) else ""

    # Always attempt the match first - including for remote postings, whose
    # PASS never depends on it but whose recorded group feeds floor selection.
    matched = None
    for city in cities:
        entry = city_lookup.get(_norm_city(city))
        if entry is not None:
            matched = {"city": city, "group": entry["group"], "via": "cities"}
            break
    if matched is None and location_text:
        located = _match_in_location(location_text, city_lookup)
        if located is not None:
            matched = {
                "city": located["city"],
                "group": located["group"],
                "via": "location",
                "location": location_text,
            }
    matched_via = matched["via"] if matched else None

    # Ruled default precedence: a remote role is a remote role wherever the
    # employer sits - it passes here either way.
    if work_mode == "remote":
        if matched is None:
            reason = "Remote role - eligible wherever the employer sits."
        elif matched["via"] == "cities":
            reason = (
                f"Remote role - eligible wherever the employer sits; it also sits in "
                f"group '{matched['group']}' ('{matched['city']}', matched via the "
                f"'cities' field), which matters only if remote precedence is off."
            )
        else:
            reason = (
                f"Remote role - eligible wherever the employer sits; its location "
                f"'{matched['location']}' also names listed city '{matched['city']}' in "
                f"group '{matched['group']}' (matched via the free-text 'location' field), "
                f"which matters only if remote precedence is off."
            )
        return {
            "verdict": VERDICT_PASS,
            "matched_group": matched["group"] if matched else None,
            "remote": True,
            "matched_via": matched_via,
            "reason": reason,
        }

    if matched is not None:
        if matched["via"] == "cities":
            reason = (
                f"Location '{matched['city']}' is in group '{matched['group']}' "
                f"(matched via the structured 'cities' field)."
            )
        else:
            reason = (
                f"Location '{matched['location']}' names listed city '{matched['city']}' "
                f"in group '{matched['group']}' (matched via the free-text 'location' field)."
            )
        return {
            "verdict": VERDICT_PASS,
            "matched_group": matched["group"],
            "remote": False,
            "matched_via": matched_via,
            "reason": reason,
        }

    # Unknown ONLY when nothing at all states where this role is.
    if not cities and not location_text and work_mode is None:
        return {
            "verdict": VERDICT_UNKNOWN,
            "matched_group": None,
            "remote": False,
            "matched_via": None,
            "unresolved_geography": True,
            "reason": (
                "Cannot tell where this role is: 'cities' is empty (geography never "
                "resolved), 'location' is blank, and 'work_mode' is missing. A data gap "
                "is not a mismatch - needs review."
            ),
        }

    # Geography IS stated; it simply matches nothing the law lists. That is a
    # resolution, not a gap: fail, even if 'work_mode' is also missing.
    stated = []
    if cities:
        stated.append("'cities': " + ", ".join(str(c) for c in cities))
    if location_text:
        stated.append(f"'location': \"{location_text}\"")
    detail = "; ".join(stated)
    reason = (
        f"Geography is stated ({detail}) and matches none of the operator's city groups, "
        f"and the role is not remote."
    )
    if work_mode is None:
        reason += " ('work_mode' is missing, but the location itself IS stated.)"
    return {
        "verdict": VERDICT_FAIL,
        "matched_group": None,
        "remote": False,
        "matched_via": None,
        "reason": reason,
    }


def absolute_floor_gate(posting, law):
    """Gate 2: is a STATED salary above the absolute floor?

    The figure judged is the TOP of the stated range: a range whose top
    reaches the floor is still worth pursuing. No stated figure -> UNKNOWN
    (passes to review); a stated figure in another currency (or with no
    currency given) -> UNKNOWN naming it - conversion would be guessing.
    """
    low, high = _stated_salary_bounds(posting)
    stated = [v for v in (low, high) if v is not None]
    absolute_floor = law["absolute_floor"]

    if not stated:
        return {
            "verdict": VERDICT_UNKNOWN,
            "figure": None,
            "floor": absolute_floor,
            "note": "No salary stated ('salary_min'/'salary_max' absent) - unstated is unknown, not a failure.",
        }

    figure = max(stated)
    currency = posting.get("salary_currency")
    if currency is None:
        return {
            "verdict": VERDICT_UNKNOWN,
            "figure": figure,
            "floor": absolute_floor,
            "note": "Salary is stated but 'salary_currency' is missing - cannot compare without guessing the currency.",
        }
    if currency != law["currency"]:
        return {
            "verdict": VERDICT_UNKNOWN,
            "figure": figure,
            "floor": absolute_floor,
            "note": f"Salary stated in {currency}, but the law's floors are in {law['currency']} - never converted, needs review.",
        }

    if figure < absolute_floor:
        return {
            "verdict": VERDICT_FAIL,
            "figure": figure,
            "floor": absolute_floor,
            "note": f"Top of stated range ({figure}) is below the absolute floor ({absolute_floor}).",
        }
    return {
        "verdict": VERDICT_PASS,
        "figure": figure,
        "floor": absolute_floor,
        "note": f"Top of stated range ({figure}) reaches the absolute floor ({absolute_floor}).",
    }


def soft_floor_check(posting, law, territory):
    """Check 3 (flags only): compare the stated figure with the applicable soft floor.

    Which floor applies, in ruled order:
      - remote AND remote.precedence_over_groups true -> the REMOTE floor,
        wherever the employer sits (the ruled default);
      - otherwise a matched group's floor - INCLUDING a remote posting whose
        city sits in a group, because with precedence off the group's floor
        governs even though the role is remote;
      - remote matching no group -> falls back to the remote floor.
    'why_this_floor' names the choice and the reason in every branch. Runs
    only for postings that cleared both gates - callers guarantee that by
    passing territory verdict 'pass'.
    """
    low, high = _stated_salary_bounds(posting)
    stated = [v for v in (low, high) if v is not None]
    figure = max(stated) if stated else None

    remote = territory.get("remote", False)
    matched_group = territory.get("matched_group")
    if remote and law["remote"]["precedence_over_groups"]:
        applied = law["remote"]["soft_floor"]
        source = "remote"
        why = "the posting is remote and remote takes precedence over city groups"
    elif matched_group:
        applied = next(g["soft_floor"] for g in law["groups"] if g["name"] == matched_group)
        source = matched_group
        if remote:
            why = (
                f"the posting is in group '{source}' and remote precedence is OFF "
                f"(remote.precedence_over_groups is false), so the group's floor applies "
                f"even though the role is remote"
            )
        else:
            why = f"the posting sits in group '{source}'"
    elif remote:
        applied = law["remote"]["soft_floor"]
        source = "remote"
        why = "the posting is remote and matches no city group"
    else:
        # Unreachable via evaluate_posting (non-remote, unmatched geography
        # never clears the territory gate); kept defensive rather than wrong.
        return {
            "result": VERDICT_NOT_REACHED,
            "applied_floor": None,
            "floor_source": None,
            "shortfall": None,
            "why_this_floor": "no applicable floor (geography did not resolve)",
        }

    if figure is None:
        return {
            "result": VERDICT_UNKNOWN,
            "applied_floor": applied,
            "floor_source": source,
            "shortfall": None,
            "why_this_floor": why,
        }

    if figure < applied:
        return {
            "result": "below",
            "applied_floor": applied,
            "floor_source": source,
            "shortfall": applied - figure,
            "why_this_floor": why,
        }
    return {
        "result": "above",
        "applied_floor": applied,
        "floor_source": source,
        "shortfall": None,
        "why_this_floor": why,
    }


def evaluate_posting(posting, city_lookup, law):
    """Run the ordered gates over one posting and build its artifact record."""
    territory = territory_gate(posting, city_lookup)
    unresolved_geography = bool(territory.pop("unresolved_geography", False))

    # Ordering guarantee: a posting that fails (or goes unknown at) an earlier
    # gate is never scored by the later ones - the record says so explicitly.
    if territory["verdict"] == VERDICT_PASS:
        absolute = absolute_floor_gate(posting, law)
    else:
        absolute = {
            "verdict": VERDICT_NOT_REACHED,
            "figure": None,
            "floor": None,
            "note": f"Not reached: territory gate was {territory['verdict']} first.",
        }

    if territory["verdict"] == VERDICT_PASS and absolute["verdict"] == VERDICT_PASS:
        soft = soft_floor_check(posting, law, territory)
    else:
        blocker = "territory" if territory["verdict"] != VERDICT_PASS else "absolute-floor"
        soft = {
            "result": VERDICT_NOT_REACHED,
            "applied_floor": None,
            "floor_source": None,
            "shortfall": None,
            "why_this_floor": f"Not reached: the {blocker} gate did not pass.",
        }

    # Overall status. Flags are not failures; unknowns go to a human.
    if territory["verdict"] == VERDICT_FAIL or absolute["verdict"] == VERDICT_FAIL:
        status = "reject"
    elif territory["verdict"] == VERDICT_UNKNOWN or absolute["verdict"] == VERDICT_UNKNOWN:
        status = "review"
    elif soft["result"] == "below":
        status = "flag"
    else:
        status = "pass"

    reason_parts = [territory["reason"], absolute["note"]]
    if soft["result"] == "below":
        reason_parts.append(
            f"Soft-floor flag only: stated top ({absolute['figure']}) is {soft['shortfall']} "
            f"under the {soft['floor_source']} soft floor ({soft['applied_floor']}) - "
            f"flagged, not rejected: apply broadly and decline later."
        )
    elif status == "pass":
        reason_parts.append("Cleared every gate.")

    return {
        # Verbatim declared fields only - never enriched, never guessed.
        "id": posting.get("id"),
        "title": posting.get("title"),
        "company": posting.get("company"),
        "url": posting.get("url"),
        "status": status,
        "unresolved_geography": unresolved_geography,
        "territory": {
            "verdict": territory["verdict"],
            "matched_group": territory["matched_group"],
            # HOW the geography matched, so a reader can tell a structured
            # facet from a free-text match: 'cities', 'location', or null.
            "matched_via": territory.get("matched_via"),
            "reason": territory["reason"],
        },
        "absolute_floor": {
            "verdict": absolute["verdict"],
            "figure": absolute["figure"],
            "floor": absolute["floor"],
            "note": absolute["note"],
        },
        "soft_floor": {
            "result": soft["result"],
            "applied_floor": soft["applied_floor"],
            "floor_source": soft["floor_source"],
            "shortfall": soft["shortfall"],
            "why_this_floor": soft["why_this_floor"],
        },
        "reason": " ".join(part for part in reason_parts if part),
    }


def evaluate_document(document, law, law_path):
    """Evaluate a freehire `search --format json` document against the law."""
    if not isinstance(document, dict) or not isinstance(document.get("results"), list):
        raise GateError(
            "input must be a JSON object with a 'results' list, in the shape "
            "freehire-search's `search --format json` emits"
        )

    city_lookup = group_cities_by_norm(law)
    records = [evaluate_posting(posting, city_lookup, law) for posting in document["results"]]

    counts = {
        "considered": len(records),
        "passed": sum(1 for r in records if r["status"] in ("pass", "flag")),
        "failed": sum(1 for r in records if r["status"] == "reject"),
        "review": sum(1 for r in records if r["status"] == "review"),
        "territory_unknown": sum(1 for r in records if r["territory"]["verdict"] == VERDICT_UNKNOWN),
        "absolute_floor_unknown": sum(1 for r in records if r["absolute_floor"]["verdict"] == VERDICT_UNKNOWN),
        "soft_below_flagged": sum(1 for r in records if r["status"] == "flag"),
        "unresolved_geography": sum(1 for r in records if r["unresolved_geography"]),
    }
    return {
        "law_version": law["version"],
        # The absolute path of the law file actually read - so a run report can
        # always be traced back to the exact rules that produced it.
        "law_path": str(Path(law_path).expanduser().resolve()),
        "counts": counts,
        "postings": records,
    }


# ---------------------------------------------------------------------------
# Output plumbing
# ---------------------------------------------------------------------------

def resolve_out_path(out_arg):
    """Pick the artifact path, refusing any path inside this repository.

    Default destination is under ~/.local/share/kairi/runs/ - alongside the
    law file, deliberately outside version control. A public repository must
    not collect personal search artifacts.
    """
    if out_arg:
        path = Path(out_arg).expanduser()
    else:
        path = DEFAULT_OUT_DIR / f"gate-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise GateError(
            f"refusing to write the artifact inside the repository ({resolved}): this is a "
            f"public repo and run artifacts carry personal search data. Pass --out outside "
            f"the repo, or drop --out to use the default under {DEFAULT_OUT_DIR}."
        )
    return resolved


def build_argument_parser():
    parser = argparse.ArgumentParser(
        prog="kairi/gate.py",
        description=(
            "Enforce the operator's search law over a page of freehire search results: "
            "territory, then absolute salary floor, then soft-floor flags - pass/fail in "
            "code, before anything scores the postings. Unstated salaries and unresolved "
            "geography are 'unknown' and pass to review; only a stated salary under the "
            "absolute floor is a hard reject."
        ),
        epilog=(
            "examples:\n"
            "  python kairi/gate.py --input results.json\n"
            "  bun run .agents/skills/freehire-search/cli/src/cli.ts search -q backend | \\\n"
            "    python kairi/gate.py --out ~/runs/today.json\n"
            f"The law is read from {DEFAULT_LAW_PATH}; it must exist - the gate refuses to "
            f"invent rules. See {README_HINT}."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        help="JSON file in the freehire `search --format json` shape (default: stdin)",
    )
    parser.add_argument(
        "--law",
        metavar="FILE",
        default=str(DEFAULT_LAW_PATH),
        help=f"search-law JSON file (default: {DEFAULT_LAW_PATH})",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="artifact destination - MUST be outside this repository "
             f"(default: a timestamped file under {DEFAULT_OUT_DIR})",
    )
    return parser


def main(argv=None):
    args = build_argument_parser().parse_args(argv)

    # Safety rail first: never do work whose output would land in the repo.
    try:
        out_path = resolve_out_path(args.out)
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 1

    try:
        law = load_law(args.law)
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 1

    try:
        if args.input:
            document = json.loads(Path(args.input).read_text(encoding="utf-8"))
        elif sys.stdin.isatty():
            print(
                "gate: no --input FILE given and stdin is a terminal - pipe the freehire "
                "`search --format json` output in, or pass --input.",
                file=sys.stderr,
            )
            return 1
        else:
            document = json.load(sys.stdin)
    except OSError as exc:
        print(f"gate: could not read input: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"gate: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        artifact = evaluate_document(document, law, args.law)
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    counts = artifact["counts"]
    print(
        f"search-law gate (law v{artifact['law_version']}): considered {counts['considered']} | "
        f"cleared {counts['passed']} ({counts['soft_below_flagged']} flagged under a soft floor) | "
        f"rejected {counts['failed']} | review {counts['review']}"
    )
    print(
        f"  unknowns: territory {counts['territory_unknown']} "
        f"(unresolved geography {counts['unresolved_geography']}), absolute-floor {counts['absolute_floor_unknown']}"
    )
    print(f"artifact: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

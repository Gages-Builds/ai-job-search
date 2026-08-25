"""Tests for kairi/gate.py — the code-enforced search-law gate.

Plain English, for a non-engineer: the operator's hard job-search criteria
(where they will work, what salary is genuinely too low) used to live in prose
that an AI assistant was trusted to obey. These tests pin the program that now
enforces those criteria mechanically, one test per ruled clause:

- gates run in order (territory, then absolute salary floor, then a soft-floor
  FLAG that is never a rejection) and a posting that fails an early gate is
  never scored by the later ones;
- unstated salaries and unresolved geography are "unknown" and pass to review -
  silently dropping them would invert the operator's apply-broadly posture;
- a remote role is judged against the REMOTE floor even in a group city;
- foreign-currency figures are never converted, never compared;
- a missing law file stops everything rather than inventing thresholds;
- the run artifact is never written inside this public repository.

The law fixture is synthetic (EXAMPLE-CITY-*, round numbers) built fresh in
each test - never the operator's real file, which does not exist on CI. One
real-world-shaped multi-word city ("Fort Myers") rides along in Group A
because whitespace/case normalization and free-text location matching are only
provable against a name with more than one word.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kairi import gate

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures: a synthetic law and search results in the freehire CLI shape.
# ---------------------------------------------------------------------------

def _law(**overrides):
    """A synthetic search law mirroring the ratified structure, fake values."""
    law = {
        "version": "test-1.0.0",
        "ruled": "2000-01-01",
        "currency": "USD",
        "posture": "I would rather apply to a job and decline it than not apply to as much as possible.",
        "groups": [
            {
                "name": "Group A",
                "cities": ["EXAMPLE-CITY-1", "Fort Myers"],
                "soft_floor": 90000,
            },
            {
                "name": "Group B",
                "cities": ["EXAMPLE-CITY-2"],
                "soft_floor": 115000,
            },
        ],
        "remote": {
            "soft_floor": 75000,
            "precedence_over_groups": True,
        },
        "absolute_floor": 60000,
    }
    law.update(overrides)
    return law


def _posting(**overrides):
    """One result in the shape freehire-search `search --format json` emits."""
    posting = {
        "id": "backend-engineer-acme-ab12cd34",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "EXAMPLE-CITY-1",
        "date": "2026-08-01T00:00:00Z",
        "url": "https://boards.example.test/acme/jobs/1",
        "work_mode": "onsite",
        "regions": [],
        "countries": [],
        "cities": ["EXAMPLE-CITY-1"],
        "skills": ["go"],
        "description": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
    }
    posting.update(overrides)
    return posting


def _doc(*postings):
    return {"meta": {"count": len(postings), "page": 1, "total": len(postings)}, "results": list(postings)}


class GateRunTestCase(unittest.TestCase):
    """Shared plumbing: write law+input into a tempdir, run gate.main in-process."""

    def run_gate(self, document, law=None, out_name="artifact.json", extra_args=()):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            law_path = tmp / "search-law.json"
            law_path.write_text(json.dumps(law if law is not None else _law()), encoding="utf-8")
            input_path = tmp / "input.json"
            input_path.write_text(json.dumps(document), encoding="utf-8")
            out_path = tmp / out_name

            argv = [
                "--input", str(input_path),
                "--law", str(law_path),
                "--out", str(out_path),
                *extra_args,
            ]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = gate.main(argv)

            artifact = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else None
            return code, artifact, stdout.getvalue(), tmp

    def assert_single(self, artifact):
        records = artifact["postings"]
        if len(records) != 1:
            self.fail(f"expected exactly 1 evaluated posting, got {len(records)}")
        return records[0]


# ---------------------------------------------------------------------------
# Clause: remote precedence over city groups (the most likely thing to get wrong)
# ---------------------------------------------------------------------------

class RemotePrecedenceTests(GateRunTestCase):
    def test_remote_posting_in_group_city_is_judged_against_the_remote_floor(self):
        # 80,000 is below Group A's 90,000 soft floor but above the remote
        # 75,000 floor. Ruled verbatim: a remote role is a remote role wherever
        # the employer sits - so it must come out ABOVE, not flagged.
        posting = _posting(work_mode="remote", salary_min=80000, salary_max=80000, salary_currency="USD")
        code, artifact, _, _ = self.run_gate(_doc(posting))
        self.assertEqual(code, 0, "gate.run failed on a plain remote-in-group-city posting")
        record = self.assert_single(artifact)

        self.assertEqual(
            record["soft_floor"]["applied_floor"], 75000,
            f"posting {record['id']}: remote precedence was ignored - the remote "
            f"floor (75000) must apply even though the city is in Group A (90000)",
        )
        self.assertEqual(record["soft_floor"]["result"], "above")
        self.assertEqual(record["soft_floor"]["floor_source"], "remote")
        self.assertEqual(record["status"], "pass")
        # The remote PASS never depends on the city, but the match must still
        # be RECORDED - soft-floor selection with precedence_over_groups=false
        # reads exactly this field.
        self.assertEqual(
            (record["territory"]["matched_group"], record["territory"]["matched_via"]),
            ("Group A", "cities"),
            f"posting {record['id']}: even for a remote posting the matched group must "
            f"be recorded, not discarded when 'remote' short-circuits the gate",
        )

    def test_remote_precedence_false_falls_back_to_the_matched_group_floor(self):
        law = _law(remote={"soft_floor": 75000, "precedence_over_groups": False})
        posting = _posting(work_mode="remote", salary_min=80000, salary_max=80000, salary_currency="USD")
        _, artifact, _, _ = self.run_gate(_doc(posting), law=law)
        record = self.assert_single(artifact)
        self.assertEqual(
            (record["soft_floor"]["applied_floor"], record["soft_floor"]["floor_source"]),
            (90000, "Group A"),
            f"posting {record['id']}: with precedence_over_groups=false the matched "
            f"group's floor must be chosen and recorded as the reason",
        )
        why = record["soft_floor"]["why_this_floor"]
        self.assertIn(
            "precedence is off", why.lower(),
            f"posting {record['id']}: why_this_floor must say the group's floor was used "
            f"BECAUSE remote precedence is off - got: {why!r}",
        )
        self.assertIn("Group A", why)


# ---------------------------------------------------------------------------
# Clause: the absolute floor is hard
# ---------------------------------------------------------------------------

class AbsoluteFloorHardRejectTests(GateRunTestCase):
    def test_stated_top_below_absolute_floor_is_a_hard_fail(self):
        posting = _posting(
            cities=["EXAMPLE-CITY-2"],
            location="EXAMPLE-CITY-2",
            salary_min=40000,
            salary_max=55000,
            salary_currency="USD",
        )
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["absolute_floor"]["verdict"], "fail",
            f"posting {record['id']}: stated top 55000 is under the 60000 absolute "
            f"floor - no application, no exceptions",
        )
        self.assertEqual(record["status"], "reject")
        self.assertEqual(artifact["counts"]["failed"], 1)


# ---------------------------------------------------------------------------
# Clause: soft floors flag with the shortfall, never reject
# ---------------------------------------------------------------------------

class SoftFloorFlagsNotRejectsTests(GateRunTestCase):
    def test_below_soft_floor_above_absolute_is_flagged_with_shortfall_not_rejected(self):
        # 70,000 clears the 60,000 absolute floor but sits 20,000 under
        # Group A's 90,000 soft floor: flag carrying the shortfall.
        posting = _posting(salary_min=65000, salary_max=70000, salary_currency="USD")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(record["soft_floor"]["result"], "below")
        self.assertEqual(
            record["soft_floor"]["shortfall"], 20000,
            f"posting {record['id']}: the flag must carry the shortfall (90000-70000)",
        )
        self.assertEqual(
            record["status"], "flag",
            f"posting {record['id']}: a below-soft-floor posting is flagged, never rejected",
        )
        self.assertEqual(
            artifact["counts"]["failed"], 0,
            "a below-soft-floor posting must not be counted as failed - the posture "
            "is apply broadly and decline later",
        )
        self.assertIn("apply broadly", record["reason"])


# ---------------------------------------------------------------------------
# Clause: no stated salary resolves to unknown and passes to review
# ---------------------------------------------------------------------------

class UnstatedSalaryTests(GateRunTestCase):
    def test_no_salary_at_all_is_unknown_and_passes_to_review_never_failed(self):
        # Most postings state no salary. Dropping them as failures would invert
        # the operator's ratified posture, so: unknown -> review.
        posting = _posting(salary_min=None, salary_max=None, salary_currency=None)
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(record["absolute_floor"]["verdict"], "unknown")
        self.assertEqual(
            record["status"], "review",
            f"posting {record['id']}: an unstated salary must pass to review, not be dropped",
        )
        self.assertEqual(
            artifact["counts"]["failed"], 0,
            "unpriced postings must NOT count as failures",
        )
        self.assertEqual(artifact["counts"]["review"], 1)
        self.assertIn("salary_min", record["reason"], "the reason line should name the missing field")


# ---------------------------------------------------------------------------
# Clause: unresolved geography is unknown, not a mismatch
# ---------------------------------------------------------------------------

class UnresolvedGeographyTests(GateRunTestCase):
    def test_null_work_mode_with_empty_cities_is_unknown_and_counted(self):
        posting = _posting(work_mode=None, cities=[], location="")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "unknown",
            f"posting {record['id']}: empty 'cities' means not-resolved, not nowhere",
        )
        self.assertEqual(record["status"], "review")
        self.assertEqual(
            artifact["counts"]["failed"], 0,
            "unresolved geography must not count as a territory failure",
        )
        self.assertEqual(
            artifact["counts"]["unresolved_geography"], 1,
            "the artifact must surface how many postings had geography unresolved",
        )

    def test_city_matching_is_case_insensitive_and_whitespace_tolerant(self):
        # The SAME city the law lists, differing only in case and internal
        # whitespace. Normalization must bridge exactly that - and no more:
        # a different name is a different name, there is no fuzzy matching.
        posting = _posting(cities=["  example-city-1  "], location="  example-city-1  ")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "pass",
            f"posting {record['id']}: city matching must ignore case and extra whitespace",
        )
        self.assertEqual(
            record["territory"]["matched_via"], "cities",
            f"posting {record['id']}: a structured 'cities' match must be reported as such",
        )

        # The real-world shape this normalization exists for: a multi-word
        # city whose spacing and casing drift from the law's spelling.
        multiword = _posting(cities=["  fort   MYERS "], location="  fort   MYERS ")
        _, artifact, _, _ = self.run_gate(_doc(multiword))
        record = self.assert_single(artifact)
        self.assertEqual(
            (record["territory"]["verdict"], record["territory"]["matched_group"]),
            ("pass", "Group A"),
            f"posting {record['id']}: a multi-word city must survive whitespace/case "
            f"drift ('  fort   MYERS ' against the law's 'Fort Myers')",
        )


# ---------------------------------------------------------------------------
# Clause: resolved geography matching no group fails the territory gate
# ---------------------------------------------------------------------------

class TerritoryFailTests(GateRunTestCase):
    def test_resolved_location_outside_every_group_is_a_territory_fail(self):
        posting = _posting(cities=["UNLISTED-CITY-9"], location="UNLISTED-CITY-9", work_mode="onsite")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "fail",
            f"posting {record['id']}: geography IS resolved here, matches no group, "
            f"and the role is not remote - that is the one case that fails territory",
        )
        self.assertEqual(record["status"], "reject")


# ---------------------------------------------------------------------------
# Clause: the free-text 'location' field resolves territory too
# ---------------------------------------------------------------------------
# Live freehire data drops the structured 'cities' facet from almost every
# result and leaves 'work_mode' null - but the city is nearly always inside
# the free-text 'location' string ("US | FL | Tampa | ..."). The gate reads
# that declared field on TOKEN BOUNDARIES - never fuzzy substring matching -
# and records which field produced each match ('matched_via').

class FreeTextLocationTests(GateRunTestCase):
    def test_multi_word_city_matches_inside_free_text_location(self):
        posting = _posting(work_mode="onsite", cities=[], location="US - Florida - Fort Myers")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "pass",
            f"posting {record['id']}: a listed multi-word city must be found inside the "
            f"free-text location string ('US - Florida - Fort Myers')",
        )
        self.assertEqual(record["territory"]["matched_group"], "Group A")
        self.assertEqual(
            record["territory"]["matched_via"], "location",
            f"posting {record['id']}: a match made through the free-text location must be "
            f"reported as 'location', distinguishable from a structured 'cities' match",
        )
        self.assertIn("location", record["territory"]["reason"].lower())

    def test_longer_word_containing_a_listed_city_is_not_the_city_austintown(self):
        # Token-boundary rule, negative direction: "Austin" listed, "Austintown"
        # given. Naked substring matching would wrongly resolve this.
        law = _law(groups=[{"name": "Group A", "cities": ["Austin"], "soft_floor": 90000}])
        posting = _posting(work_mode="onsite", cities=[], location="123 Austintown Rd, OH")
        _, artifact, _, _ = self.run_gate(_doc(posting), law=law)
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "fail",
            f"posting {record['id']}: 'Austintown' contains 'Austin' but is not Austin - "
            f"a longer word is a different place, no substring matching",
        )
        self.assertIsNone(record["territory"]["matched_via"])

    def test_longer_word_containing_a_listed_city_is_not_the_city_provost(self):
        law = _law(groups=[{"name": "Group B", "cities": ["Provo"], "soft_floor": 115000}])
        posting = _posting(work_mode="onsite", cities=[], location="Provost Road")
        _, artifact, _, _ = self.run_gate(_doc(posting), law=law)
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "fail",
            f"posting {record['id']}: 'Provost Road' contains 'Provo' but is not Provo",
        )

    def test_present_location_matching_no_law_city_fails_even_when_work_mode_is_null(self):
        # "NASA Goddard" states geography precisely; it simply is nowhere the
        # operator works. Stated-and-unmatched is FAIL, never UNKNOWN - and
        # live data leaves work_mode null on nearly everything.
        posting = _posting(work_mode=None, cities=[], location="NASA Goddard")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["territory"]["verdict"], "fail",
            f"posting {record['id']}: the geography IS stated here - a miss is a mismatch, "
            f"not an unresolved gap",
        )
        self.assertFalse(record["unresolved_geography"])
        self.assertIsNone(record["territory"]["matched_via"])
        self.assertEqual(record["status"], "reject")

    def test_blank_location_with_no_cities_and_null_work_mode_stays_unknown(self):
        # Whitespace-only location counts as absent: nothing resolves geography.
        posting = _posting(work_mode=None, cities=[], location="   ")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(record["territory"]["verdict"], "unknown")
        self.assertTrue(record["unresolved_geography"])
        self.assertEqual(record["status"], "review")

    def test_remote_posting_whose_group_city_appears_only_in_location_records_both(self):
        # FIX 1 + FIX 3 together: remote passes regardless, but the group match
        # found through the free-text location is still recorded - and with the
        # default law (precedence true) the REMOTE floor still applies.
        posting = _posting(
            work_mode="remote",
            cities=[],
            location="Acme Corp - US - Florida - Fort Myers campus",
            salary_min=95000,
            salary_max=95000,
            salary_currency="USD",
        )
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(record["territory"]["verdict"], "pass")
        self.assertEqual(record["territory"]["matched_group"], "Group A")
        self.assertEqual(record["territory"]["matched_via"], "location")
        self.assertEqual(
            (record["soft_floor"]["floor_source"], record["soft_floor"]["applied_floor"]),
            ("remote", 75000),
            f"posting {record['id']}: with remote precedence ON the remote floor applies "
            f"even though a group was matched via the location text",
        )


# ---------------------------------------------------------------------------
# Clause: foreign currency is unknown, named, never converted
# ---------------------------------------------------------------------------

class ForeignCurrencyTests(GateRunTestCase):
    def test_salary_in_foreign_currency_is_unknown_with_the_currency_named(self):
        posting = _posting(salary_min=100000, salary_max=100000, salary_currency="EUR")
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["absolute_floor"]["verdict"], "unknown",
            f"posting {record['id']}: a EUR figure must never be silently compared with USD floors",
        )
        self.assertIn(
            "EUR", record["absolute_floor"]["note"],
            f"posting {record['id']}: the unknown note must name the currency actually stated",
        )
        self.assertEqual(record["status"], "review")

    def test_stated_salary_with_missing_currency_is_unknown_not_compared(self):
        posting = _posting(salary_min=100000, salary_max=100000, salary_currency=None)
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(
            record["absolute_floor"]["verdict"], "unknown",
            f"posting {record['id']}: comparing a figure with no currency would mean guessing",
        )


# ---------------------------------------------------------------------------
# Clause: the gate refuses to run without the real law
# ---------------------------------------------------------------------------

class LawFileRequiredTests(unittest.TestCase):
    def test_missing_law_file_exits_non_zero_and_names_the_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "nowhere" / "search-law.json"
            input_path = Path(tmpdir) / "input.json"
            input_path.write_text(json.dumps(_doc(_posting())), encoding="utf-8")
            out_path = Path(tmpdir) / "artifact.json"

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = gate.main([
                    "--input", str(input_path),
                    "--law", str(missing),
                    "--out", str(out_path),
                ])

            self.assertNotEqual(
                code, 0,
                "the gate must exit non-zero when the law file is absent",
            )
            output = stderr.getvalue()
            self.assertIn(
                str(missing), output,
                "the error message must name the law path that was looked for",
            )
            self.assertIn("README", output, "the error message must point at kairi/README.md")
            self.assertFalse(out_path.exists(), "no artifact may be written when the law is missing")

    def test_malformed_law_json_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_law = Path(tmpdir) / "bad-law.json"
            bad_law.write_text("{not json", encoding="utf-8")
            input_path = Path(tmpdir) / "input.json"
            input_path.write_text(json.dumps(_doc(_posting())), encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = gate.main([
                    "--input", str(input_path),
                    "--law", str(bad_law),
                    "--out", str(Path(tmpdir) / "artifact.json"),
                ])
            self.assertNotEqual(code, 0, "malformed law JSON must stop the run")
            self.assertIn(str(bad_law), stderr.getvalue())

    def test_law_missing_a_required_key_is_rejected(self):
        law = _law()
        del law["absolute_floor"]
        with tempfile.TemporaryDirectory() as tmpdir:
            law_path = Path(tmpdir) / "law.json"
            law_path.write_text(json.dumps(law), encoding="utf-8")
            input_path = Path(tmpdir) / "input.json"
            input_path.write_text(json.dumps(_doc()), encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = gate.main([
                    "--input", str(input_path),
                    "--law", str(law_path),
                    "--out", str(Path(tmpdir) / "artifact.json"),
                ])
            self.assertNotEqual(code, 0, "a law without its absolute floor is not a law")
            self.assertIn("absolute_floor", stderr.getvalue())


# ---------------------------------------------------------------------------
# Clause: artifacts stay out of the repository
# ---------------------------------------------------------------------------

class ArtifactLocationGuardTests(unittest.TestCase):
    def test_gate_refuses_to_write_its_artifact_inside_the_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            law_path = Path(tmpdir) / "law.json"
            law_path.write_text(json.dumps(_law()), encoding="utf-8")
            input_path = Path(tmpdir) / "input.json"
            input_path.write_text(json.dumps(_doc(_posting())), encoding="utf-8")
            unsafe_out = REPO_ROOT / "kairi" / "would-be-artifact.json"

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = gate.main([
                    "--input", str(input_path),
                    "--law", str(law_path),
                    "--out", str(unsafe_out),
                ])

            self.assertNotEqual(
                code, 0,
                "writing the artifact inside the public repository must be refused",
            )
            explanation = stderr.getvalue()
            self.assertIn("refusing", explanation.lower())
            self.assertFalse(
                unsafe_out.exists(),
                "the refused artifact must not exist afterwards",
            )


# ---------------------------------------------------------------------------
# Clause: gate ordering - failing an early gate means never being scored
# ---------------------------------------------------------------------------

class GateOrderingTests(GateRunTestCase):
    def test_territory_failure_means_absolute_and_soft_are_marked_not_reached(self):
        posting = _posting(
            id="outside-city-lowsalary-deadbeef",
            cities=["UNLISTED-CITY-9"],
            location="UNLISTED-CITY-9",
            salary_min=10000,
            salary_max=20000,
            salary_currency="USD",
        )
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)

        self.assertEqual(
            record["absolute_floor"]["verdict"], "not_reached",
            f"posting {record['id']}: a territory-failed posting must never be scored "
            f"by the absolute-floor gate",
        )
        self.assertEqual(
            record["soft_floor"]["result"], "not_reached",
            f"posting {record['id']}: its soft-floor field must record that scoring "
            f"was never reached",
        )
        self.assertIsNone(
            record["soft_floor"]["applied_floor"],
            f"posting {record['id']}: no applied floor may appear when the gates stopped early",
        )

    def test_unknown_at_absolute_floor_also_leaves_soft_floor_unreached(self):
        posting = _posting(salary_min=None, salary_max=None, salary_currency=None)
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertEqual(record["absolute_floor"]["verdict"], "unknown")
        self.assertEqual(
            record["soft_floor"]["result"], "not_reached",
            f"posting {record['id']}: with no comparable figure there is nothing to flag",
        )


# ---------------------------------------------------------------------------
# Artifact shape: traceability and honest counts
# ---------------------------------------------------------------------------

class ArtifactShapeTests(GateRunTestCase):
    def test_artifact_records_law_version_and_absolute_law_path(self):
        posting = _posting(work_mode="remote", salary_min=90000, salary_max=90000, salary_currency="USD")
        code, artifact, _, _ = self.run_gate(_doc(posting))
        self.assertEqual(code, 0)
        self.assertEqual(artifact["law_version"], "test-1.0.0")
        self.assertTrue(Path(artifact["law_path"]).is_absolute())
        self.assertIn("search-law.json", artifact["law_path"])

    def test_counts_partition_considered_into_passed_failed_review(self):
        document = _doc(
            _posting(id="pass-one", salary_max=120000, salary_currency="USD"),
            _posting(id="flag-one", salary_max=70000, salary_currency="USD"),
            # Geography stated and unmatched in BOTH places the gate reads -
            # otherwise the free-text 'location' match would resolve it.
            _posting(id="reject-one", cities=["UNLISTED-CITY-9"], location="UNLISTED-CITY-9", work_mode="onsite"),
            _posting(id="review-one", salary_min=None, salary_max=None),
        )
        _, artifact, _, _ = self.run_gate(document)
        counts = artifact["counts"]
        self.assertEqual(counts["considered"], 4)
        self.assertEqual(counts["passed"], 2, "one clean pass plus one soft-flag both cleared every gate")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["review"], 1)
        self.assertEqual(counts["soft_below_flagged"], 1)
        self.assertEqual(counts["passed"] + counts["failed"] + counts["review"], counts["considered"])

    def test_no_value_is_invented_for_fields_the_input_lacks(self):
        posting = _posting(company=None)
        _, artifact, _, _ = self.run_gate(_doc(posting))
        record = self.assert_single(artifact)
        self.assertIsNone(
            record["company"],
            f"posting {record['id']}: a company the input did not state must stay null, never defaulted",
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("test_shard_filter.py")
SPEC = importlib.util.spec_from_file_location("test_shard_filter", SCRIPT_PATH)
SHARD_FILTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARD_FILTER)


def timing_data(**overrides):
    data = {
        "schemaVersion": 1,
        "generatedAtUtc": "2026-08-15T00:00:00Z",
        "discoveryCommit": "abc",
        "sourceRunIds": [1],
        "profileRuns": 1,
        "timingModel": "test",
        "defaultCaseSeconds": 1.0,
        "defaultMethodSeconds": 1.0,
        "methodCaseSeconds": {},
        "caseSeconds": {},
    }
    data.update(overrides)
    return data


class ShardFilterTests(unittest.TestCase):
    def test_load_timings_rejects_invalid_schema_and_seconds(self):
        for data in (
            timing_data(schemaVersion=2),
            timing_data(defaultCaseSeconds=0),
            timing_data(caseSeconds={"Suite.Test": -1}),
        ):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "timings.json")
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    SHARD_FILTER.load_timings(path)

    def test_parses_discovered_tests(self):
        lines = [
            "noise",
            "The following Tests are available:",
            "    Suite.Test(1)",
            "    Suite.Test(2)",
            "done",
        ]
        self.assertEqual(
            SHARD_FILTER.parse_tests(lines),
            ["Suite.Test(1)", "Suite.Test(2)"],
        )

    def test_builds_exact_filters_and_runsettings(self):
        groups = [
            ("Alpha.Suite", "Plain", None),
            ("Beta.Suite", "Cases", "Beta.Suite.Cases(1)"),
        ]
        self.assertEqual(
            SHARD_FILTER.build_filter(groups),
            "(class=='Alpha.Suite'&&method=='Plain')||test=='Beta.Suite.Cases(1)'",
        )
        self.assertIn(
            "<AssemblySelectLimit>100000</AssemblySelectLimit>",
            SHARD_FILTER.build_runsettings("method=='Plain'"),
        )

    def test_splits_parameterized_method_that_exceeds_worker_budget(self):
        tests = ["Suite.Case(1)", "Suite.Case(2)", "Other.Plain"]
        timings = timing_data(
            defaultCaseSeconds=0.1,
            defaultMethodSeconds=0.1,
            caseSeconds={tests[0]: 10.0, tests[1]: 10.0, tests[2]: 30.0},
        )
        groups, _ = SHARD_FILTER.extract_groups(tests, timings, 2)
        case_groups = [group for group in groups if group[1] == "Case"]
        self.assertEqual(len(case_groups), 2)
        self.assertTrue(all(group[2] is not None for group in case_groups))

    def test_builds_profile_and_dual_validation_matrices(self):
        self.assertEqual(
            SHARD_FILTER.build_shard_matrix(2),
            {"include": [{"shard": 0}, {"shard": 1}]},
        )
        self.assertEqual(
            SHARD_FILTER.build_validation_matrix(2),
            {
                "include": [
                    {"model": "total", "shard": 0},
                    {"model": "total", "shard": 1},
                    {"model": "body", "shard": 0},
                    {"model": "body", "shard": 1},
                ]
            },
        )

    def test_collects_nunit_durations_and_pair_context(self):
        output = """Creating a new pair, no suitable pair found in pool
Retrieving pair 1 from pool took 2500 ms
Retrieving pair 2 from pool took 500 ms"""
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<test-run duration="7.5">
  <test-suite>
    <test-case fullname="Suite.Passed" result="Passed" duration="3.25">
      <output>{output}</output>
    </test-case>
    <test-case fullname="Suite.Skipped" result="Skipped" duration="0" />
    <test-case fullname="Suite.Broken" result="Passed" duration="bad" />
  </test-suite>
</test-run>
"""
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "results.xml").write_text(xml, encoding="utf-8")
            Path(directory, "gravestone.xml").write_text(
                "<graveyard><pair /></graveyard>",
                encoding="utf-8",
            )
            results = SHARD_FILTER.collect_nunit_results(directory)

        self.assertEqual(results["caseSeconds"], {"Suite.Passed": 3.25})
        self.assertEqual(results["poolSeconds"], {"Suite.Passed": 3.0})
        self.assertEqual(results["contextPoolSeconds"], {"Suite.Passed": 2.5})
        self.assertEqual(results["contextPairCounts"], {"Suite.Passed": 1})
        self.assertEqual(results["notExecuted"], ["Suite.Skipped"])
        self.assertEqual(results["wallSeconds"], 7.5)

    def test_builds_positive_timing_config(self):
        config, missing = SHARD_FILTER.build_timing_config(
            ["Suite.Fast", "Suite.Missing"],
            {"Suite.Fast": {1: 0.0000001}},
            1,
            "abc",
            [123],
            "total",
        )
        self.assertEqual(config["timingModel"], "total")
        self.assertGreater(config["caseSeconds"]["Suite.Fast"], 0)
        self.assertEqual(missing, ["Suite.Missing"])

    def test_load_observations_can_exclude_context_creation(self):
        sample = {
            "profileRun": 1,
            "shard": 0,
            "successful": True,
            "caseSeconds": {"Suite.Test": 10.0},
            "poolSeconds": {"Suite.Test": 6.0},
            "contextPoolSeconds": {"Suite.Test": 5.0},
            "contextPairCounts": {"Suite.Test": 1},
            "discardedPairCounts": {},
            "notExecuted": [],
            "wallSeconds": 12.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sample.json").write_text(json.dumps(sample), encoding="utf-8")
            total, _, completed = SHARD_FILTER.load_observations(
                directory,
                ["Suite.Test"],
            )
            body, _, _ = SHARD_FILTER.load_observations(
                directory,
                ["Suite.Test"],
                exclude_context_creation=True,
            )

        self.assertEqual(total["Suite.Test"][1], 10.0)
        self.assertEqual(body["Suite.Test"][1], 5.0)
        self.assertEqual(completed, {(1, 0)})

    def test_load_observations_ignores_failed_and_invalid_samples(self):
        valid = {
            "profileRun": 1,
            "shard": 0,
            "successful": False,
            "caseSeconds": {"Suite.Test": 1.0},
            "poolSeconds": {},
            "contextPoolSeconds": {},
            "contextPairCounts": {},
            "discardedPairCounts": {},
            "notExecuted": [],
            "wallSeconds": 2.0,
        }
        invalid_samples = [
            {**valid, "successful": True, "profileRun": 0},
            {**valid, "successful": True, "profileRun": True},
            {**valid, "successful": True, "caseSeconds": None},
        ]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "failed.json").write_text(json.dumps(valid), encoding="utf-8")
            for index, sample in enumerate(invalid_samples):
                Path(directory, f"invalid-{index}.json").write_text(
                    json.dumps(sample),
                    encoding="utf-8",
                )
            observations, _, completed = SHARD_FILTER.load_observations(
                directory,
                ["Suite.Test"],
            )

        self.assertEqual(observations, {})
        self.assertEqual(completed, set())

    def test_only_consistently_skipped_tests_are_omitted(self):
        omitted = SHARD_FILTER.find_omitted_tests(
            ["Suite.Skipped", "Suite.Missing"],
            {},
            {"Suite.Skipped": {1, 2}},
            2,
        )
        self.assertEqual(omitted, {"Suite.Skipped"})

    def test_summarizes_validation_medians(self):
        samples = {
            (run, shard): seconds
            for run, values in {1: (10.0, 12.0), 2: (11.0, 13.0), 3: (9.0, 14.0)}.items()
            for shard, seconds in enumerate(values)
        }
        medians, ratio, missing = SHARD_FILTER.summarize_validation(samples, 3, 2)
        self.assertEqual(medians, {0: 10.0, 1: 13.0})
        self.assertTrue(math.isclose(ratio, 1.3))
        self.assertEqual(missing, [])

        del samples[3, 1]
        medians, ratio, missing = SHARD_FILTER.summarize_validation(samples, 3, 2)
        self.assertEqual(medians, {0: 10.0, 1: 12.5})
        self.assertTrue(math.isclose(ratio, 1.25))
        self.assertEqual(missing, [(3, 1)])


if __name__ == "__main__":
    unittest.main()

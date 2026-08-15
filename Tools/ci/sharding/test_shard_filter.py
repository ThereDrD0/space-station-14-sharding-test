#!/usr/bin/env python3

"""Распределяет интеграционные тесты по шардам и собирает длительности из NUnit3 XML."""

import json
import math
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


PARAMETERIZED_CASE_SPLIT_THRESHOLD = 256
NUNIT_WORKERS = 2
MAX_VALIDATION_SHARD_RATIO = 1.25
TIMINGS_SCHEMA_VERSION = 1
MIN_RECORDED_SECONDS = 0.000001
TIMINGS_PATH = Path(__file__).with_name("integration_test_timings.json")
PAIR_RETRIEVAL_PATTERN = re.compile(
    r"Retrieving pair \d+ from pool took ([0-9]+(?:\.[0-9]+)?) ms"
)
CONTEXT_PAIR_CREATION = "Creating a new pair, no suitable pair found in pool"
SETTINGS_PAIR_CREATION = "Creating pair, because settings of pool settings"
PAIR_DISCARDED = "CleanReturnAsync: Clean disposed in"


def parse_tests(lines):
    """Извлекает полные имена из вывода `dotnet test --list-tests`."""
    list_headers = {
        "The following Tests are available:",
        "Доступны следующие тесты:",
    }
    tests = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped in list_headers:
            in_list = True
            continue
        if not in_list:
            continue
        if not stripped:
            continue
        if not line[:1].isspace():
            break
        tests.append(stripped)
    return tests


def split_test_name(test):
    """Возвращает имя класса, метода и полное имя метода NUnit."""
    name = test.split("(", 1)[0].strip()
    dot = name.rfind(".")
    fixture = name[:dot] if dot > 0 else ""
    method = name[dot + 1:] if dot > 0 else name
    full_method = ".".join(part for part in (fixture, method) if part)
    return fixture, method, full_method


def _positive_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def load_timings(path=TIMINGS_PATH):
    """Загружает конфигурацию секунд и отклоняет повреждённые данные."""
    try:
        with open(path, encoding="utf-8") as file:
            timings = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read integration test timings from {path}: {error}") from error

    if not isinstance(timings, dict) or timings.get("schemaVersion") != TIMINGS_SCHEMA_VERSION:
        raise ValueError(
            f"integration test timings must use schemaVersion {TIMINGS_SCHEMA_VERSION}"
        )

    for field in ("generatedAtUtc", "discoveryCommit"):
        if not isinstance(timings.get(field), str) or not timings[field]:
            raise ValueError(f"integration test timings field {field} must be a non-empty string")
    if (
        not isinstance(timings.get("profileRuns"), int)
        or isinstance(timings["profileRuns"], bool)
        or timings["profileRuns"] <= 0
    ):
        raise ValueError("integration test timings field profileRuns must be a positive integer")
    source_run_ids = timings.get("sourceRunIds")
    if not isinstance(source_run_ids, list) or not source_run_ids or any(
        not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0
        for run_id in source_run_ids
    ):
        raise ValueError("integration test timings field sourceRunIds must be an array of run IDs")

    for field in ("defaultCaseSeconds", "defaultMethodSeconds"):
        if not _positive_number(timings.get(field)):
            raise ValueError(f"integration test timings field {field} must be a positive number")

    for field in ("methodCaseSeconds", "caseSeconds"):
        values = timings.get(field)
        if not isinstance(values, dict):
            raise ValueError(f"integration test timings field {field} must be an object")
        if any(not isinstance(name, str) or not _positive_number(seconds) for name, seconds in values.items()):
            raise ValueError(
                f"integration test timings field {field} must map names to positive seconds"
            )

    return timings


def extract_groups(tests, timings, total_shards):
    """Группирует методы, а тяжёлые параметризованные методы делит по кейсам."""
    method_cases = {}
    for test in tests:
        fixture, method, full_method = split_test_name(test)
        method_cases.setdefault((fixture, method, full_method), []).append(test)

    estimates = {}
    total_seconds = 0.0
    for key, cases in method_cases.items():
        full_method = key[2]
        method_default = timings["methodCaseSeconds"].get(full_method)
        exact = timings["caseSeconds"]

        if method_default is None and not any(test in exact for test in cases):
            method_total = max(
                timings["defaultMethodSeconds"],
                len(cases) * timings["defaultCaseSeconds"],
            )
            case_estimates = [method_total / len(cases)] * len(cases)
        else:
            fallback = method_default or timings["defaultCaseSeconds"]
            case_estimates = [exact.get(test, fallback) for test in cases]

        estimates[key] = case_estimates
        total_seconds += sum(case_estimates)

    target_seconds = total_seconds / total_shards
    group_counts = {}
    group_seconds = {}
    for (fixture, method, full_method), cases in method_cases.items():
        case_estimates = estimates[(fixture, method, full_method)]
        split_cases = len(set(cases)) > 1 and (
            len(cases) > PARAMETERIZED_CASE_SPLIT_THRESHOLD
            or sum(case_estimates) > target_seconds
        )

        if split_cases:
            for test, seconds in zip(cases, case_estimates):
                group = (fixture, method, test)
                group_counts[group] = group_counts.get(group, 0) + 1
                group_seconds[group] = group_seconds.get(group, 0.0) + seconds
            continue

        group = (fixture, method, None)
        group_counts[group] = len(cases)
        group_seconds[group] = sum(case_estimates)

    return group_counts, group_seconds


def quote_tsl(value):
    """Экранирует значение для NUnit Test Selection Language."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_filter(groups):
    """Строит точный NUnit.Where для методов и отдельных тест-кейсов."""
    if not groups:
        return ""

    expressions = []
    for fixture, method, test in sorted(
        groups,
        key=lambda group: (group[0], group[1], group[2] or ""),
    ):
        if test is not None:
            expressions.append(f"test=='{quote_tsl(test)}'")
            continue

        method_expr = f"method=='{quote_tsl(method)}'"
        if fixture:
            expressions.append(f"(class=='{quote_tsl(fixture)}'&&{method_expr})")
        else:
            expressions.append(method_expr)

    return "||".join(expressions)


def build_runsettings(filter_expr):
    """Строит runsettings с фильтром адаптера NUnit."""
    if not filter_expr:
        filter_expr = "method=='__no_tests_assigned__'"

    return f"""<?xml version="1.0" encoding="utf-8"?>
<RunSettings>
  <NUnit>
    <DisplayName>FullName</DisplayName>
    <MapWarningTo>Failed</MapWarningTo>
    <Where>{escape(filter_expr)}</Where>
  </NUnit>
</RunSettings>
"""


def estimate_shard_seconds(groups, group_seconds):
    """Оценивает время шарда с двумя потоками и последовательными фикстурами."""
    fixture_seconds = {}
    for group in groups:
        fixture = group[0] or f"{group[1]}:{group[2] or ''}"
        fixture_seconds[fixture] = fixture_seconds.get(fixture, 0.0) + group_seconds[group]

    workers = [0.0] * NUNIT_WORKERS
    for seconds in sorted(fixture_seconds.values(), reverse=True):
        worker = min(range(NUNIT_WORKERS), key=lambda index: (workers[index], index))
        workers[worker] += seconds
    return max(workers, default=0.0)


def distribute_groups(group_counts, group_seconds, total):
    """Распределяет группы по минимальному ожидаемому времени завершения шарда."""
    shards = [[] for _ in range(total)]
    shard_seconds = [0.0] * total
    shard_totals = [0.0] * total

    for group in sorted(
        group_counts,
        key=lambda item: (
            -group_seconds[item],
            item[0],
            item[1],
            item[2] or "",
        ),
    ):
        def placement_score(shard):
            candidate_seconds = estimate_shard_seconds(
                [*shards[shard], group],
                group_seconds,
            )
            slowest = max(
                candidate_seconds if index == shard else shard_seconds[index]
                for index in range(total)
            )
            return slowest, candidate_seconds, shard_totals[shard], shard

        lightest = min(range(total), key=placement_score)
        shards[lightest].append(group)
        shard_totals[lightest] += group_seconds[group]
        shard_seconds[lightest] = estimate_shard_seconds(
            shards[lightest],
            group_seconds,
        )

    return shards, shard_seconds


def build_shard_matrix(total_shards):
    """Строит по одному долгоживущему заданию на каждый шард."""
    return {"include": [{"shard": shard} for shard in range(total_shards)]}


def build_validation_matrix(total_shards):
    """Строит задания проверки для обоих вариантов учёта времени."""
    return {
        "include": [
            {"model": model, "shard": shard}
            for model in ("total", "body")
            for shard in range(total_shards)
        ]
    }


def collect_nunit_results(directory):
    """Собирает длительности кейсов, пула и всего запуска из NUnit3 XML."""
    case_seconds = {}
    pool_seconds = {}
    context_pool_seconds = {}
    context_pair_counts = {}
    discarded_pair_counts = {}
    not_executed = set()
    run_seconds = []
    for path in Path(directory).rglob("*.xml"):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as error:
            print(f"Warning: cannot parse {path}: {error}", file=sys.stderr)
            continue

        if root.tag != "test-run":
            continue

        duration = root.get("duration")
        try:
            if duration is not None and _positive_number(float(duration)):
                run_seconds.append(float(duration))
        except ValueError:
            print(f"Warning: cannot parse run duration in {path}", file=sys.stderr)

        for element in root.iter("test-case"):
            name = element.get("fullname")
            result = element.get("result")
            if name and result not in ("Passed", "Failed"):
                not_executed.add(name)
                continue
            if not name:
                continue
            duration = element.get("duration")
            if duration is None:
                continue
            try:
                seconds = float(duration)
            except ValueError:
                print(
                    f"Warning: cannot parse duration for {name} in {path}",
                    file=sys.stderr,
                )
                continue
            if not _positive_number(seconds):
                continue
            case_seconds[name] = seconds

            output = element.findtext("output", default="")
            retrievals = list(PAIR_RETRIEVAL_PATTERN.finditer(output))
            if retrievals:
                pool_seconds[name] = sum(
                    float(match.group(1)) for match in retrievals
                ) / 1000
                context_seconds = 0.0
                context_count = 0
                previous_end = 0
                for match in retrievals:
                    if CONTEXT_PAIR_CREATION in output[previous_end:match.start()]:
                        context_seconds += float(match.group(1)) / 1000
                        context_count += 1
                    previous_end = match.end()
                if context_count:
                    context_pool_seconds[name] = context_seconds
                    context_pair_counts[name] = context_count

            discarded = max(
                output.count(PAIR_DISCARDED) - output.count(SETTINGS_PAIR_CREATION),
                0,
            )
            if discarded:
                discarded_pair_counts[name] = discarded

    return {
        "caseSeconds": case_seconds,
        "poolSeconds": pool_seconds,
        "contextPoolSeconds": context_pool_seconds,
        "contextPairCounts": context_pair_counts,
        "discardedPairCounts": discarded_pair_counts,
        "notExecuted": sorted(not_executed),
        "wallSeconds": max(run_seconds) if run_seconds else None,
    }


def _trimmed_mean(values):
    ordered = sorted(values)
    trim = len(ordered) // 10
    if trim:
        ordered = ordered[trim:-trim]
    return statistics.fmean(ordered)


def _percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _round_seconds(seconds):
    return max(round(seconds, 6), MIN_RECORDED_SECONDS)


def build_timing_config(
    tests,
    observations,
    profile_runs,
    commit,
    source_run_ids,
    timing_model,
    skipped_tests=(),
):
    """Строит итоговую конфигурацию по наблюдениям отдельных запусков."""
    required_observations = math.ceil(profile_runs * 0.8)
    case_seconds = {
        test: _trimmed_mean(list(observations[test].values()))
        for test in sorted(set(tests))
        if len(observations.get(test, {})) >= required_observations
    }
    if not case_seconds:
        raise ValueError("no tests have enough successful timing observations")

    method_cases = {}
    for test in tests:
        method_cases.setdefault(split_test_name(test)[2], []).append(test)

    method_case_seconds = {}
    method_totals = []
    for method, cases in method_cases.items():
        measured = [case_seconds[test] for test in cases if test in case_seconds]
        if not measured:
            continue
        fallback = statistics.median(measured)
        method_case_seconds[method] = fallback
        method_totals.append(sum(case_seconds.get(test, fallback) for test in cases))

    rounded_cases = {
        name: _round_seconds(seconds)
        for name, seconds in case_seconds.items()
    }
    rounded_methods = {
        name: _round_seconds(seconds)
        for name, seconds in sorted(method_case_seconds.items())
    }
    config = {
        "schemaVersion": TIMINGS_SCHEMA_VERSION,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "discoveryCommit": commit,
        "sourceRunIds": source_run_ids,
        "profileRuns": profile_runs,
        "timingModel": timing_model,
        "defaultCaseSeconds": _round_seconds(
            _percentile(list(case_seconds.values()), 0.75)
        ),
        "defaultMethodSeconds": _round_seconds(_percentile(method_totals, 0.75)),
        "methodCaseSeconds": rounded_methods,
        "caseSeconds": rounded_cases,
    }
    missing = sorted(set(tests) - case_seconds.keys() - set(skipped_tests))
    return config, missing


def load_observations(directory, tests, exclude_context_creation=False):
    """Объединяет результаты разных шардов по тесту и номеру повтора."""
    test_set = set(tests)
    observations = {}
    skipped_observations = {}
    completed_shard_runs = set()
    for path in Path(directory).glob("*.json"):
        try:
            sample = json.loads(path.read_text(encoding="utf-8"))
            profile_run = sample["profileRun"]
            shard = sample["shard"]
            successful = sample["successful"]
            values = sample["caseSeconds"]
            pool_values = sample["poolSeconds"]
            context_pool_values = sample["contextPoolSeconds"]
            context_pair_counts = sample["contextPairCounts"]
            discarded_pair_counts = sample["discardedPairCounts"]
            not_executed = sample["notExecuted"]
            wall_seconds = sample["wallSeconds"]
            if (
                not isinstance(profile_run, int)
                or isinstance(profile_run, bool)
                or profile_run <= 0
            ):
                raise ValueError("profileRun must be a positive integer")
            if (
                not isinstance(shard, int)
                or isinstance(shard, bool)
                or shard < 0
            ):
                raise ValueError("shard must be a non-negative integer")
            if not isinstance(successful, bool):
                raise ValueError("successful must be a boolean")
            if not isinstance(values, dict):
                raise ValueError("caseSeconds must be an object")
            if not isinstance(pool_values, dict):
                raise ValueError("poolSeconds must be an object")
            if not isinstance(context_pool_values, dict):
                raise ValueError("contextPoolSeconds must be an object")
            if not isinstance(context_pair_counts, dict):
                raise ValueError("contextPairCounts must be an object")
            if not isinstance(discarded_pair_counts, dict):
                raise ValueError("discardedPairCounts must be an object")
            if not isinstance(not_executed, list) or any(
                not isinstance(test, str) for test in not_executed
            ):
                raise ValueError("notExecuted must be an array of test names")
            if not _positive_number(wall_seconds):
                raise ValueError("wallSeconds must be a positive number")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Warning: ignoring invalid sample {path}: {error}", file=sys.stderr)
            continue
        if not successful:
            continue
        completed_shard_runs.add((profile_run, shard))
        for test, seconds in values.items():
            if test in test_set and _positive_number(seconds):
                pool_seconds = pool_values.get(test, 0.0)
                if not isinstance(pool_seconds, (int, float)) or isinstance(pool_seconds, bool):
                    continue
                if not math.isfinite(pool_seconds) or pool_seconds < 0:
                    continue
                context_pool_seconds = context_pool_values.get(test, 0.0)
                if (
                    not isinstance(context_pool_seconds, (int, float))
                    or isinstance(context_pool_seconds, bool)
                    or not math.isfinite(context_pool_seconds)
                    or context_pool_seconds < 0
                ):
                    continue
                context_pair_count = context_pair_counts.get(test, 0)
                discarded_pair_count = discarded_pair_counts.get(test, 0)
                if (
                    not isinstance(context_pair_count, int)
                    or isinstance(context_pair_count, bool)
                    or context_pair_count < 0
                    or not isinstance(discarded_pair_count, int)
                    or isinstance(discarded_pair_count, bool)
                    or discarded_pair_count < 0
                    or context_pool_seconds > pool_seconds
                    or bool(context_pool_seconds) != bool(context_pair_count)
                ):
                    continue
                measured_seconds = seconds
                if exclude_context_creation:
                    measured_seconds -= context_pool_seconds
                observations.setdefault(test, {}).setdefault(
                    profile_run,
                    max(measured_seconds, MIN_RECORDED_SECONDS),
                )
        for test in not_executed:
            if test in test_set:
                skipped_observations.setdefault(test, set()).add(profile_run)
    return observations, skipped_observations, completed_shard_runs


def find_incomplete_profile_shards(completed_shard_runs, profile_runs, total_shards):
    """Находит шарды без достаточного числа завершённых измерений."""
    required_observations = math.ceil(profile_runs * 0.8)
    incomplete = {}
    for shard in range(total_shards):
        completed = sum(
            (profile_run, shard) in completed_shard_runs
            for profile_run in range(1, profile_runs + 1)
        )
        if completed < required_observations:
            incomplete[shard] = completed
    return incomplete


def find_omitted_tests(
    tests,
    observations,
    skipped_observations,
    required_observations,
):
    """Находит незапускаемые NUnit-тесты с Ignore или Explicit."""
    omitted = set()
    for test in set(tests):
        if observations.get(test):
            continue
        skipped_runs = skipped_observations.get(test, set())
        if len(skipped_runs) >= required_observations:
            omitted.add(test)
    return omitted


def _parse_positive_int(value, name):
    try:
        parsed = int(value)
    except ValueError:
        print(f"Error: {name} must be a positive integer", file=sys.stderr)
        sys.exit(1)
    if parsed <= 0:
        print(f"Error: {name} must be a positive integer", file=sys.stderr)
        sys.exit(1)
    return parsed


def _read_generation_inputs(timings_path=TIMINGS_PATH):
    tests = parse_tests(sys.stdin.read().splitlines())
    if not tests:
        print("Error: no tests discovered from input", file=sys.stderr)
        sys.exit(1)

    try:
        timings = load_timings(timings_path)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    return tests, timings


def _write_shards(
    tests,
    timings,
    total,
    output_dir,
    distributor=distribute_groups,
):
    group_counts, group_seconds = extract_groups(tests, timings, total)
    print(
        f"Discovered {len(tests)} tests in {len(group_counts)} groups, "
        f"distributing across {total} shards",
        file=sys.stderr,
    )

    os.makedirs(output_dir, exist_ok=True)
    shards, shard_seconds = distributor(
        group_counts,
        group_seconds,
        total,
    )

    for shard in range(total):
        my_groups = sorted(
            shards[shard],
            key=lambda group: (group[0], group[1], group[2] or ""),
        )
        path = os.path.join(output_dir, f"shard_{shard}.runsettings")
        with open(path, "w", encoding="utf-8") as file:
            file.write(build_runsettings(build_filter(my_groups)))
        print(
            f"  Shard {shard}: {len(my_groups)} groups, "
            f"{shard_seconds[shard]:.1f} estimated seconds "
            f"({sum(group_counts[group] for group in my_groups)} tests)",
            file=sys.stderr,
        )


def cmd_generate():
    if len(sys.argv) not in (4, 5):
        print(
            f"Usage: {sys.argv[0]} generate <total-shards> <output-dir> "
            "[timings-json]",
            file=sys.stderr,
        )
        sys.exit(1)

    total = _parse_positive_int(sys.argv[2], "total-shards")
    timings_path = Path(sys.argv[4]) if len(sys.argv) == 5 else TIMINGS_PATH
    tests, timings = _read_generation_inputs(timings_path)
    _write_shards(tests, timings, total, sys.argv[3])


def cmd_generate_profile():
    if len(sys.argv) not in (4, 5):
        print(
            f"Usage: {sys.argv[0]} generate-profile <total-shards> <output-dir> "
            "[timings-json]",
            file=sys.stderr,
        )
        sys.exit(1)

    total = _parse_positive_int(sys.argv[2], "total-shards")
    timings_path = Path(sys.argv[4]) if len(sys.argv) == 5 else TIMINGS_PATH
    tests, timings = _read_generation_inputs(timings_path)
    _write_shards(tests, timings, total, sys.argv[3])


def cmd_matrix():
    if len(sys.argv) != 7:
        print(
            f"Usage: {sys.argv[0]} matrix <profile-runs> <max-parallel> "
            "<profile-shards> <validation-shards> <github-output>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        profile_runs = int(sys.argv[2])
        max_parallel = int(sys.argv[3])
        profile_shards = int(sys.argv[4])
        validation_shards = int(sys.argv[5])
    except ValueError:
        print(
            "Error: profile-runs, max-parallel and shard counts must be integers",
            file=sys.stderr,
        )
        sys.exit(1)

    if profile_runs not in (10, 20, 30, 50):
        print("Error: profile-runs must be one of: 10, 20, 30, 50", file=sys.stderr)
        sys.exit(1)
    if profile_shards <= 0 or validation_shards <= 0:
        print("Error: shard counts must be positive integers", file=sys.stderr)
        sys.exit(1)
    if not 1 <= max_parallel <= profile_shards:
        print(
            f"Error: max-parallel must be between 1 and {profile_shards}",
            file=sys.stderr,
        )
        sys.exit(1)

    matrix = build_shard_matrix(profile_shards)
    validation_matrix = build_validation_matrix(validation_shards)
    with open(sys.argv[6], "a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
        output.write(
            "validation_matrix="
            f"{json.dumps(validation_matrix, separators=(',', ':'))}\n"
        )
        output.write(f"profile_runs={profile_runs}\n")
        output.write(f"max_parallel={max_parallel}\n")


def cmd_collect():
    if len(sys.argv) != 7:
        print(
            f"Usage: {sys.argv[0]} collect <profile-run> <shard> <status> "
            "<nunit-results-dir> <output-json>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        profile_run = int(sys.argv[2])
        shard = int(sys.argv[3])
    except ValueError:
        print("Error: profile-run and shard must be integers", file=sys.stderr)
        sys.exit(1)
    if profile_run <= 0 or shard < 0:
        print("Error: profile-run must be positive and shard non-negative", file=sys.stderr)
        sys.exit(1)
    status = sys.argv[4]
    if status not in ("success", "failure"):
        print("Error: status must be success or failure", file=sys.stderr)
        sys.exit(1)

    output = Path(sys.argv[6])
    output.parent.mkdir(parents=True, exist_ok=True)
    data = collect_nunit_results(sys.argv[5])
    data.update({
        "profileRun": profile_run,
        "shard": shard,
        "successful": status == "success",
    })
    output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"Collected {len(data['caseSeconds'])} test durations", file=sys.stderr)


def cmd_aggregate():
    if len(sys.argv) != 10:
        print(
            f"Usage: {sys.argv[0]} aggregate <discovery-log> <samples-dir> "
            "<total-output-json> <body-output-json> <profile-runs> <commit> "
            "<source-run-id> <total-shards>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        profile_runs = int(sys.argv[6])
        source_run_id = int(sys.argv[8])
        total_shards = int(sys.argv[9])
    except ValueError:
        print(
            "Error: profile-runs, source-run-id and total-shards must be integers",
            file=sys.stderr,
        )
        sys.exit(1)
    if profile_runs <= 0 or total_shards <= 0:
        print("Error: profile-runs and total-shards must be positive", file=sys.stderr)
        sys.exit(1)

    discovery = Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace")
    tests = parse_tests(discovery.splitlines())
    if not tests:
        print("Error: no tests discovered from discovery log", file=sys.stderr)
        sys.exit(1)
    observations, skipped_observations, completed_shard_runs = load_observations(
        sys.argv[3],
        tests,
    )
    body_observations, _, _ = load_observations(
        sys.argv[3],
        tests,
        exclude_context_creation=True,
    )
    required_observations = math.ceil(profile_runs * 0.8)
    incomplete_shards = find_incomplete_profile_shards(
        completed_shard_runs,
        profile_runs,
        total_shards,
    )
    if incomplete_shards:
        details = ", ".join(
            f"{shard}: {completed}/{profile_runs}"
            for shard, completed in incomplete_shards.items()
        )
        print(
            f"Error: not enough completed profile runs for shards {details}; "
            f"at least {required_observations} required",
            file=sys.stderr,
        )
        sys.exit(1)
    skipped_tests = find_omitted_tests(
        tests,
        observations,
        skipped_observations,
        required_observations,
    )

    try:
        total_config, missing = build_timing_config(
            tests,
            observations,
            profile_runs,
            sys.argv[7],
            [source_run_id],
            "total",
            skipped_tests,
        )
        body_config, body_missing = build_timing_config(
            tests,
            body_observations,
            profile_runs,
            sys.argv[7],
            [source_run_id],
            "bodyWithoutContextCreation",
            skipped_tests,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    for output_path, config in (
        (sys.argv[4], total_config),
        (sys.argv[5], body_config),
    ):
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print("## Профиль времени интеграционных тестов")
    print()
    print(f"Измерено тест-кейсов: {len(total_config['caseSeconds'])} из {len(set(tests))}.")
    print(f"Полных повторов: {profile_runs}; минимум успешных наблюдений: {math.ceil(profile_runs * 0.8)}.")
    print(f"Пропущено NUnit: {len(skipped_tests)}.")
    print("Сформированы два профиля: полное время и время без контекстного создания пары.")
    print()
    print("### Самые долгие тест-кейсы")
    print()
    print("| Тест | Среднее после отсечения |")
    print("| --- | ---: |")
    for test, seconds in sorted(
        total_config["caseSeconds"].items(),
        key=lambda item: item[1],
        reverse=True,
    )[:20]:
        escaped_test = test.replace("|", "\\|")
        print(f"| `{escaped_test}` | {seconds:.3f} с |")
    if missing:
        print()
        print("### Недостаточно успешных наблюдений")
        print()
        print(f"Таких тест-кейсов: {len(missing)}. Для них сработают значения метода или общие значения.")
        for test in missing[:20]:
            print(f"- `{test}`")
    if body_missing != missing:
        print("::warning title=Профили расходятся::Для двух моделей различается набор измеренных тестов.")


def load_validation_samples(directory, validation_runs, total_shards, model):
    samples = {}
    for path in Path(directory).glob(f"integration-validation-{model}-*.json"):
        try:
            sample = json.loads(path.read_text(encoding="utf-8"))
            validation_run = sample["profileRun"]
            shard = sample["shard"]
            successful = sample["successful"]
            wall_seconds = sample["wallSeconds"]
            if (
                not isinstance(validation_run, int)
                or isinstance(validation_run, bool)
                or not 1 <= validation_run <= validation_runs
            ):
                raise ValueError("profileRun is outside validation range")
            if (
                not isinstance(shard, int)
                or isinstance(shard, bool)
                or not 0 <= shard < total_shards
            ):
                raise ValueError("shard is outside validation range")
            if successful is not True:
                raise ValueError("test shard did not complete successfully")
            if not _positive_number(wall_seconds):
                raise ValueError("wallSeconds must be a positive number")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Warning: ignoring invalid validation sample {path}: {error}", file=sys.stderr)
            continue
        samples.setdefault((validation_run, shard), wall_seconds)
    return samples


def summarize_validation(samples, validation_runs, total_shards):
    expected = {
        (validation_run, shard)
        for validation_run in range(1, validation_runs + 1)
        for shard in range(total_shards)
    }
    missing = sorted(expected - samples.keys())
    medians = {
        shard: statistics.median(
            samples[validation_run, shard]
            for validation_run in range(1, validation_runs + 1)
        )
        for shard in range(total_shards)
        if all(
            (validation_run, shard) in samples
            for validation_run in range(1, validation_runs + 1)
        )
    }
    ratio = max(medians.values()) / min(medians.values()) if medians else math.inf
    return medians, ratio, missing


def cmd_validate():
    if len(sys.argv) != 6:
        print(
            f"Usage: {sys.argv[0]} validate <samples-dir> "
            "<validation-runs> <total-shards> <model>",
            file=sys.stderr,
        )
        sys.exit(1)

    validation_runs = _parse_positive_int(sys.argv[3], "validation-runs")
    total_shards = _parse_positive_int(sys.argv[4], "total-shards")
    model = sys.argv[5]
    if model not in ("total", "body"):
        print("Error: model must be total or body", file=sys.stderr)
        sys.exit(1)
    samples = load_validation_samples(
        sys.argv[2],
        validation_runs,
        total_shards,
        model,
    )
    medians, ratio, missing = summarize_validation(
        samples,
        validation_runs,
        total_shards,
    )

    print(f"## Проверка баланса интеграционных шардов: {model}")
    print()
    print("| Шард | Запуски | Медиана |")
    print("| ---: | --- | ---: |")
    for shard in range(total_shards):
        values = [
            samples.get((validation_run, shard))
            for validation_run in range(1, validation_runs + 1)
        ]
        formatted = ", ".join(
            f"{seconds:.1f} с" if seconds is not None else "нет"
            for seconds in values
        )
        median = medians.get(shard)
        print(f"| {shard} | {formatted} | {median:.1f} с |" if median else f"| {shard} | {formatted} | нет |")
    print()
    print(f"Отношение самого медленного шарда к самому быстрому: {ratio:.3f}.")

    if missing:
        print(f"::error title=Неполная проверка шардов::Не хватает {len(missing)} успешных запусков.")
        sys.exit(1)
    if ratio > MAX_VALIDATION_SHARD_RATIO:
        print(
            "::warning title=Шарды распределены неровно::"
            f"Отношение {ratio:.3f} превышает {MAX_VALIDATION_SHARD_RATIO:.2f}."
        )


def cmd_choose():
    if len(sys.argv) != 9:
        print(
            f"Usage: {sys.argv[0]} choose <samples-dir> <validation-runs> "
            "<total-shards> <total-json> <body-json> <output-json> <github-output>",
            file=sys.stderr,
        )
        sys.exit(1)

    validation_runs = _parse_positive_int(sys.argv[3], "validation-runs")
    total_shards = _parse_positive_int(sys.argv[4], "total-shards")
    ratios = {}
    for model in ("total", "body"):
        samples = load_validation_samples(
            sys.argv[2],
            validation_runs,
            total_shards,
            model,
        )
        _, ratio, missing = summarize_validation(samples, validation_runs, total_shards)
        if missing:
            print(f"Error: {model} is missing {len(missing)} validation runs", file=sys.stderr)
            sys.exit(1)
        ratios[model] = ratio

    selected = min(ratios, key=ratios.get)
    source = Path(sys.argv[5] if selected == "total" else sys.argv[6])
    output = Path(sys.argv[7])
    output.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with open(sys.argv[8], "a", encoding="utf-8") as github_output:
        github_output.write(f"selected_model={selected}\n")
        github_output.write(f"total_ratio={ratios['total']:.6f}\n")
        github_output.write(f"body_ratio={ratios['body']:.6f}\n")
    print(
        f"Выбран профиль {selected}: total={ratios['total']:.3f}, "
        f"body={ratios['body']:.3f}."
    )


def cmd_read():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} read <runsettings-file>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[2]
    if not os.path.exists(path):
        return
    root = ET.parse(path).getroot()
    where = root.findtext("./NUnit/Where", default="").strip()
    if where:
        print("Running filtered test groups from the generated shard.", file=sys.stderr)
        print(where)


def main():
    if len(sys.argv) < 2:
        print(
            f"Usage: {sys.argv[0]} "
            "<generate|generate-profile|matrix|collect|aggregate|validate|choose|read> ...",
            file=sys.stderr,
        )
        sys.exit(1)

    commands = {
        "generate": cmd_generate,
        "generate-profile": cmd_generate_profile,
        "matrix": cmd_matrix,
        "collect": cmd_collect,
        "aggregate": cmd_aggregate,
        "validate": cmd_validate,
        "choose": cmd_choose,
        "read": cmd_read,
    }
    command = commands.get(sys.argv[1])
    if command is None:
        print(f"Unknown command: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)
    command()


if __name__ == "__main__":
    main()

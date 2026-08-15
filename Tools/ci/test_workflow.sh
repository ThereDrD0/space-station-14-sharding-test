#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHARDING_SCRIPT="$ROOT_DIR/Tools/ci/sharding/test_shard_filter.py"
RESULTS_DIR=/tmp/test-results
PROFILE_SHARD_COUNT=8
PROFILE_MEASUREMENT_SHARD_COUNT=8
cd "$ROOT_DIR"

setup_root_submodules() {
    git submodule update --init --recursive
}

setup_engine_submodules() {
    git -C RobustToolbox submodule update --init --recursive
}

restore_integration() {
    dotnet restore Content.IntegrationTests/Content.IntegrationTests.csproj
}

build_integration() {
    dotnet build Content.IntegrationTests/Content.IntegrationTests.csproj \
        --configuration DebugOpt --no-restore /m
}

discover_shards() {
    local shard_count="${1:?Не указано количество шардов}"
    dotnet test --list-tests --no-build --no-restore --configuration DebugOpt \
        Content.IntegrationTests/Content.IntegrationTests.csproj \
        -- NUnit.DisplayName=FullName 2>&1 \
        | python3 "$SHARDING_SCRIPT" generate "$shard_count" .integration-filters
}

discover_profile() {
    dotnet test --list-tests --no-build --no-restore --configuration DebugOpt \
        Content.IntegrationTests/Content.IntegrationTests.csproj \
        -- NUnit.DisplayName=FullName 2>&1 \
        | tee integration-test-discovery.log \
        | python3 "$SHARDING_SCRIPT" generate-profile \
            "$PROFILE_MEASUREMENT_SHARD_COUNT" .integration-profile-filters
}

prune_build() {
    local variant="${1:?Не указан вариант артефакта}"
    test "$PWD" = "$GITHUB_WORKSPACE"
    test -f bin/Content.Client/Content.Client.dll
    test -f bin/Content.Server/Content.Server.dll
    test -f bin/Content.IntegrationTests/Content.IntegrationTests.dll

    find . -path './.git' -prune -o -type d -name obj -prune -exec rm -rf -- {} +
    find . -path './.git' -prune -o -type d -name bin ! -path './bin' -prune -exec rm -rf -- {} +

    case "$variant" in
        profile|integration)
            find bin -mindepth 1 -maxdepth 1 -type d \
                ! -name Content.IntegrationTests \
                -exec rm -rf -- {} +
            ;;
        *)
            echo "Неизвестный вариант артефакта: $variant" >&2
            return 1
            ;;
    esac

    # Пустые каталоги нужны ResourceManager как точки монтирования сборок.
    mkdir -p bin/Content.Client bin/Content.Server
}

archive_build() {
    local variant="${1:?Не указан вариант артефакта}"
    case "$variant" in
        profile)
            mkdir -p /tmp/integration-profile-build/Tools/ci
            cp Tools/ci/test_workflow.sh \
                /tmp/integration-profile-build/Tools/ci/test_workflow.sh
            tar -I 'zstd -T0 -3' -cf /tmp/integration-profile-build/integration-profile-build.tar.zst \
                Resources RobustToolbox/Resources .integration-profile-filters Tools/ci \
                bin/Content.Client bin/Content.Server bin/Content.IntegrationTests
            ;;
        integration)
            mkdir -p /tmp/integration-shard-build/Tools/ci
            cp Tools/ci/test_workflow.sh \
                /tmp/integration-shard-build/Tools/ci/test_workflow.sh
            tar -I 'zstd -T0 -3' -cf /tmp/integration-shard-build/integration-shard-build.tar.zst \
                Resources RobustToolbox/Resources .integration-filters Tools/ci \
                bin/Content.Client bin/Content.Server bin/Content.IntegrationTests
            ;;
        *)
            echo "Неизвестный вариант артефакта: $variant" >&2
            return 1
            ;;
    esac
}

extract_build() {
    local archive="${1:?Не указан архив сборки}"
    test -f "$archive"
    tar --zstd -xf "$archive"
    rm -- "$archive"
}

run_integration_shard() {
    : "${SHARD:?Не указан номер шарда}"
    [[ "$SHARD" =~ ^[0-9]+$ ]]
    local settings=".integration-filters/shard_${SHARD}.runsettings"
    mkdir -p "$RESULTS_DIR"
    timeout --signal=TERM --kill-after=2m 15m \
        dotnet test bin/Content.IntegrationTests/Content.IntegrationTests.dll \
        --settings "$settings" \
        --logger "console;verbosity=normal" \
        --blame-hang --blame-hang-timeout 6min --blame-hang-dump-type mini \
        -- NUnit.ConsoleOut=0 NUnit.MapWarningTo=Failed NUnit.TestOutputXml="logs" \
        NUnit.WorkDirectory="$RESULTS_DIR"
}

prepare_profile_matrix() {
    python3 "$SHARDING_SCRIPT" matrix \
        "$PROFILE_RUNS" "$MAX_PARALLEL_RUNNERS" "$PROFILE_MEASUREMENT_SHARD_COUNT" \
        "$PROFILE_SHARD_COUNT" "$GITHUB_OUTPUT"
}

prepare_sharded_run() {
    [[ "$MAX_PARALLEL_RUNNERS" =~ ^[1-8]$ ]] || {
        echo "Максимальное число раннеров должно быть от 1 до 8." >&2
        return 1
    }
    echo "max_parallel=$MAX_PARALLEL_RUNNERS" >> "$GITHUB_OUTPUT"
}

test_sharding_script() {
    python3 Tools/ci/sharding/test_shard_filter_tests.py
}

run_profile_shard() {
    : "${SHARD:?Не указан номер шарда}"
    [[ "$SHARD" =~ ^[0-9]+$ ]]
    local settings=".integration-profile-filters/shard_${SHARD}.runsettings"
    mkdir -p "$RESULTS_DIR"
    timeout --signal=TERM --kill-after=2m 15m \
        dotnet test bin/Content.IntegrationTests/Content.IntegrationTests.dll \
        --settings "$settings" \
        --logger "console;verbosity=minimal" \
        -- NUnit.ConsoleOut=0 NUnit.MapWarningTo=Failed NUnit.TestOutputXml="logs" \
        NUnit.WorkDirectory="$RESULTS_DIR"
}

run_validation_shard() {
    : "${SHARD:?Не указан номер шарда}"
    [[ "$SHARD" =~ ^[0-9]+$ ]]
    : "${PROFILE_MODEL:?Не указана модель времени}"
    local settings=".integration-validation-filters-${PROFILE_MODEL}/shard_${SHARD}.runsettings"
    mkdir -p "$RESULTS_DIR"
    timeout --signal=TERM --kill-after=2m 15m \
        dotnet test bin/Content.IntegrationTests/Content.IntegrationTests.dll \
        --settings "$settings" \
        --logger "console;verbosity=minimal" \
        -- NUnit.ConsoleOut=0 NUnit.MapWarningTo=Failed NUnit.TestOutputXml="logs" \
        NUnit.WorkDirectory="$RESULTS_DIR"
}

collect_profile() {
    : "${PROFILE_RUN:?Не указан номер повтора}"
    : "${SHARD:?Не указан номер шарда}"
    : "${TEST_OUTCOME:?Не указан результат запуска тестов}"
    local sample_prefix="${SAMPLE_PREFIX:-integration-timing}"
    python3 "$SHARDING_SCRIPT" collect \
        "$PROFILE_RUN" "$SHARD" "$TEST_OUTCOME" "$RESULTS_DIR" \
        "/tmp/${sample_prefix}-${PROFILE_RUN}-${SHARD}.json"
}

run_profile_repetitions() {
    : "${PROFILE_RUNS:?Не указано количество повторов}"
    : "${PROFILE_MODE:?Не указан режим профилирования}"
    : "${SAMPLE_PREFIX:?Не указан префикс образца}"
    : "${SHARD:?Не указан номер шарда}"
    [[ "$PROFILE_RUNS" =~ ^[1-9][0-9]*$ ]]
    [[ "$SHARD" =~ ^[0-9]+$ ]]
    [[ "$PROFILE_MODE" == profile || "$PROFILE_MODE" == validation ]]

    for ((PROFILE_RUN = 1; PROFILE_RUN <= PROFILE_RUNS; PROFILE_RUN++)); do
        RESULTS_DIR="/tmp/test-results-${SAMPLE_PREFIX}-${PROFILE_RUN}-${SHARD}"
        if [[ "$PROFILE_MODE" == profile ]]; then
            if run_profile_shard; then
                TEST_OUTCOME=success
            else
                TEST_OUTCOME=failure
            fi
        elif run_validation_shard; then
            TEST_OUTCOME=success
        else
            TEST_OUTCOME=failure
        fi
        collect_profile
    done
}

aggregate_profile() {
    python3 "$SHARDING_SCRIPT" aggregate \
        integration-test-discovery.log integration-timing-samples \
        Tools/ci/sharding/integration_test_timings_total.json \
        Tools/ci/sharding/integration_test_timings_body.json "$PROFILE_RUNS" \
        "$GITHUB_SHA" "$GITHUB_RUN_ID" "$PROFILE_MEASUREMENT_SHARD_COUNT" \
        | tee -a "$GITHUB_STEP_SUMMARY"
    python3 "$SHARDING_SCRIPT" generate \
        "$PROFILE_SHARD_COUNT" .integration-validation-filters-total \
        Tools/ci/sharding/integration_test_timings_total.json \
        < integration-test-discovery.log
    python3 "$SHARDING_SCRIPT" generate \
        "$PROFILE_SHARD_COUNT" .integration-validation-filters-body \
        Tools/ci/sharding/integration_test_timings_body.json \
        < integration-test-discovery.log
}

validate_profile() {
    python3 "$SHARDING_SCRIPT" validate \
        integration-validation-samples "$PROFILE_VALIDATION_RUNS" "$PROFILE_SHARD_COUNT" total \
        | tee -a "$GITHUB_STEP_SUMMARY"
    python3 "$SHARDING_SCRIPT" validate \
        integration-validation-samples "$PROFILE_VALIDATION_RUNS" "$PROFILE_SHARD_COUNT" body \
        | tee -a "$GITHUB_STEP_SUMMARY"
    python3 "$SHARDING_SCRIPT" choose \
        integration-validation-samples "$PROFILE_VALIDATION_RUNS" "$PROFILE_SHARD_COUNT" \
        Tools/ci/sharding/integration_test_timings_total.json \
        Tools/ci/sharding/integration_test_timings_body.json \
        Tools/ci/sharding/integration_test_timings.json "$GITHUB_OUTPUT" \
        | tee -a "$GITHUB_STEP_SUMMARY"
}

command="${1:-}"
shift || true
case "$command" in
    setup-root-submodules) setup_root_submodules "$@" ;;
    setup-engine-submodules) setup_engine_submodules "$@" ;;
    restore-integration) restore_integration "$@" ;;
    build-integration) build_integration "$@" ;;
    discover-shards) discover_shards "$@" ;;
    discover-profile) discover_profile "$@" ;;
    prune-build) prune_build "$@" ;;
    archive-build) archive_build "$@" ;;
    extract-build) extract_build "$@" ;;
    run-integration-shard) run_integration_shard "$@" ;;
    test-sharding-script) test_sharding_script "$@" ;;
    prepare-profile-matrix) prepare_profile_matrix "$@" ;;
    prepare-sharded-run) prepare_sharded_run "$@" ;;
    run-profile-shard) run_profile_shard "$@" ;;
    run-validation-shard) run_validation_shard "$@" ;;
    collect-profile) collect_profile "$@" ;;
    run-profile-repetitions) run_profile_repetitions "$@" ;;
    aggregate-profile) aggregate_profile "$@" ;;
    validate-profile) validate_profile "$@" ;;
    *)
        echo "Неизвестная команда test_workflow.sh: $command" >&2
        exit 1
        ;;
esac

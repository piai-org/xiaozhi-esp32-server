#!/usr/bin/env bash
# Host-side runner: creates isolated MySQL/Redis, runs ALL Java tests, then removes its own resources.
# No production credentials, Docker network, data volume or published ports are used.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
run_id="miniapp-test-$(date +%Y%m%d%H%M%S)-$$"
test_net="$run_id-net"
test_db="$run_id-db"
test_redis="$run_id-redis"
test_volume="$run_id-data"
build_name="$run_id-build"
cache_volume=xiaozhi-miniapp-maven-cache
maven_image=maven:3.9.9-eclipse-temurin-21
report_dir="${MINIAPP_TEST_REPORT_DIR:-$repo_root/main/manager-api/.test-reports/$run_id}"
mkdir -p "$report_dir"

docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then docker_cmd=(sudo -n docker); fi
"${docker_cmd[@]}" info >/dev/null
cleanup() {
    "${docker_cmd[@]}" rm -f "$build_name" "$test_db" "$test_redis" >/dev/null 2>&1 || true
    "${docker_cmd[@]}" network rm "$test_net" >/dev/null 2>&1 || true
    "${docker_cmd[@]}" volume rm "$test_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${docker_cmd[@]}" network create "$test_net" >/dev/null
"${docker_cmd[@]}" volume create "$test_volume" >/dev/null
"${docker_cmd[@]}" volume create "$cache_volume" >/dev/null
"${docker_cmd[@]}" run --rm -v "$cache_volume:/tmp/m2" "$maven_image" \
    chown -R "$(id -u):$(id -g)" /tmp/m2
"${docker_cmd[@]}" run -d --name "$test_db" --network "$test_net" --network-alias test-db \
    --memory=512m --cpus=1 -e MYSQL_ROOT_PASSWORD=isolated-test-only -e MYSQL_DATABASE=xiaozhi_miniapp_test \
    -v "$test_volume:/var/lib/mysql" mysql:8.4 \
    --innodb-buffer-pool-size=128M --max-connections=30 --performance-schema=OFF >/dev/null
"${docker_cmd[@]}" run -d --name "$test_redis" --network "$test_net" --network-alias test-redis \
    --memory=64m --cpus=0.25 redis:8.0 redis-server --maxmemory 32mb --maxmemory-policy noeviction >/dev/null

ready=false
for ((attempt=0; attempt<60; attempt++)); do
    if "${docker_cmd[@]}" exec -e MYSQL_PWD=isolated-test-only "$test_db" \
            mysql -uroot -e 'SELECT 1' >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
done
if [[ "$ready" != true ]]; then echo 'Isolated MySQL did not become ready' >&2; exit 1; fi
"${docker_cmd[@]}" exec -e MYSQL_PWD=isolated-test-only "$test_db" mysql -uroot \
    -e 'CREATE DATABASE xiaozhi_miniapp_regression CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;'

echo "Running complete Java test suite in isolation; report directory: $report_dir"
set +e
"${docker_cmd[@]}" run --rm --name "$build_name" --network "$test_net" --memory=1100m --cpus=1.5 \
    --user "$(id -u):$(id -g)" -e MAVEN_CONFIG=/tmp/m2 -e MAVEN_OPTS=-Xmx320m \
    -e 'JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2 -Xmx512m' \
    -e 'MINIAPP_TEST_JDBC_URL=jdbc:mysql://test-db:3306/xiaozhi_miniapp_test?serverTimezone=UTC&allowPublicKeyRetrieval=true&useSSL=false' \
    -v "$repo_root:/workspace" -v "$cache_volume:/tmp/m2" -w /workspace/main/manager-api "$maven_image" \
    mvn -B -Dmaven.repo.local=/tmp/m2/repository -DskipTests=false \
    '-Dspring.datasource.druid.url=jdbc:mysql://test-db:3306/xiaozhi_miniapp_regression?serverTimezone=UTC&allowPublicKeyRetrieval=true&useSSL=false' \
    -Dspring.datasource.druid.username=root -Dspring.datasource.druid.password=isolated-test-only \
    -Dspring.datasource.druid.initial-size=1 -Dspring.datasource.druid.min-idle=1 -Dspring.datasource.druid.max-active=10 \
    -Dspring.data.redis.host=test-redis -Dspring.data.redis.port=6379 -Dspring.data.redis.password= \
    clean verify >"$report_dir/maven.log" 2>&1
test_status=$?
set -e
if [[ -d "$repo_root/main/manager-api/target/surefire-reports" ]]; then
    cp -R "$repo_root/main/manager-api/target/surefire-reports" "$report_dir/"
fi
tail -n 16 "$report_dir/maven.log"
if [[ $test_status -ne 0 ]]; then exit "$test_status"; fi

# The opt-in MySQL/HTTP integration tests must actually execute, not merely be skipped.
python3 - "$report_dir" <<'PY'
import glob, json, os, sys, xml.etree.ElementTree as ET
root = sys.argv[1]
reports = [ET.parse(path).getroot() for path in glob.glob(root + '/surefire-reports/TEST-*.xml')]
app = [report for report in reports if '.modules.app.' in report.attrib['name']]
assert app and all(int(report.attrib['skipped']) == 0 for report in app), 'Miniapp integration tests were skipped'
summary = {key: sum(int(report.attrib[key]) for report in reports) for key in ('tests','failures','errors','skipped')}
summary['miniapp_tests'] = sum(int(report.attrib['tests']) for report in app)
with open(root + '/summary.json', 'w') as output:
    json.dump(summary, output, indent=2)
print(json.dumps(summary))
PY

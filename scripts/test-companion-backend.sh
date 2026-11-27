#!/usr/bin/env bash
# Bind a clean checkout only. No operator config, network, production volume or credentials.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
voice_image=${COMPANION_TEST_IMAGE:-ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server@sha256:ae9eb9ab6f621e54a82f39f1ab81aa24425ed490bb405e9154cee396dc3c9fbe}
docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then docker_cmd=(sudo -n docker); fi
"${docker_cmd[@]}" run --rm --network none --memory=1000m --cpus=2     --read-only --tmpfs /tmp:rw,size=256m -e PYTHONDONTWRITEBYTECODE=1     -v "$repo_root/main/xiaozhi-server:/workspace:ro" -w /workspace     --entrypoint python "$voice_image" scripts/test-companion.py
# Parse the browser modules using Node's module syntax, without installing packages.
if command -v node >/dev/null; then
    while IFS= read -r -d '' source; do node --input-type=module --check < "$source"; done         < <(find "$repo_root/main/digital-human/js" -name '*.js' -print0)
else
    echo 'Node is unavailable; browser syntax check was not run.' >&2
fi

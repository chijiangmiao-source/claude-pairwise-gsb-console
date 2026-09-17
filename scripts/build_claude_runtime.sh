#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_IMAGE="${PAIRWISE_CLAUDE_BASE_IMAGE:-claude-eval-runtime:claude-2.1.269}"
TARGET_IMAGE="${PAIRWISE_CLAUDE_IMAGE:-claude-eval-runtime:prepared-2.1.269}"

if [[ "$BASE_IMAGE" == "$TARGET_IMAGE" ]]; then
  echo "Base and prepared Claude image tags must be different." >&2
  exit 1
fi
docker image inspect "$BASE_IMAGE" >/dev/null
docker build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --file "$ROOT/docker/ClaudeRuntime.Dockerfile" \
  --tag "$TARGET_IMAGE" \
  "$ROOT/docker"
echo "Prepared Claude image: $TARGET_IMAGE"

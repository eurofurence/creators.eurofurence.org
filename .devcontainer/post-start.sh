#!/usr/bin/env bash

set -e

if ! docker network inspect creators-dev >/dev/null 2>&1; then
    docker network create creators-dev >/dev/null
fi

docker compose up -d db s3

docker network connect creators-dev "$(hostname)" 2>/dev/null || true

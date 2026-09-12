#!/usr/bin/env bash

set -e

docker compose up -d db s3

docker network connect creators-dev "$(hostname)" 2>/dev/null || true

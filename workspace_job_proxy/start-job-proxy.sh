#!/bin/sh
set -e

: "${WORKSPACE_JOB_PROXY_HOST:=127.0.0.1}"
: "${WORKSPACE_JOB_PROXY_PORT:=9000}"

uvicorn workspace_job_proxy.job_proxy:app \
    --host "$WORKSPACE_JOB_PROXY_HOST" \
    --port "$WORKSPACE_JOB_PROXY_PORT" \
    --workers 1 &


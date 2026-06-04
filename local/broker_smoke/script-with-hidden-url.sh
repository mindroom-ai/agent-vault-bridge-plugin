#!/bin/sh
set -eu

python /app/local/broker_smoke/request_headers.py http://upstream:8080/headers

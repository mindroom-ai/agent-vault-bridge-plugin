#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
url="${BROKER_SMOKE_UPSTREAM_URL:-http://upstream:8080/headers}"
python_bin="${PYTHON:-python}"

"$python_bin" "$script_dir/request_headers.py" "$url"

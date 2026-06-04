from __future__ import annotations

import sys
import urllib.request


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: request_headers.py URL", file=sys.stderr)
        raise SystemExit(2)

    with urllib.request.urlopen(sys.argv[1], timeout=10) as response:
        sys.stdout.write(response.read().decode("utf-8"))
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()

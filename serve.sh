#!/usr/bin/env bash
# Start the service by hand: the news API, the review desk's API, and the daily scrape.
# Starts the database first if it is stopped. Extra arguments go to `avn serve`.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source scripts/ensure-db.sh
exec .venv/bin/avn serve "$@"

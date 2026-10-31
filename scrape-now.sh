#!/usr/bin/env bash
# Scrape now and save to Postgres, without the service. Starts the database if it
# is stopped and looks back to the last full run, at least window_hours. If the
# service is already scraping, this stops with a message. Extra arguments go to `avn run`.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source scripts/ensure-db.sh
exec .venv/bin/avn run --since-last-run "$@"

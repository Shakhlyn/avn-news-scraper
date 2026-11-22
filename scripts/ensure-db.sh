# Sourced by scrape-now.sh and serve.sh: make sure the database is up. Docker may
# still be starting after a boot, so it gets two minutes. Each docker call gets a time
# limit too, so a stuck Docker daemon fails here instead of hanging the script.
CONTAINER=avn-postgres
DOCKER_BIN=$(command -v docker || echo docker)
docker() { timeout 20 "$DOCKER_BIN" "$@"; }

for _ in $(seq 120); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker, then run this again." >&2
  exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "No $CONTAINER container. Run docker compose up -d in avn_postgres_db/pg-stack." >&2
  exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
  echo "Starting $CONTAINER"
  docker start "$CONTAINER" >/dev/null
fi
for _ in $(seq 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER")" = "healthy" ] && break
  sleep 1
done
if [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER")" != "healthy" ]; then
  echo "$CONTAINER did not become healthy in 60 seconds." >&2
  exit 1
fi

#!/usr/bin/env bash
#
# In-place deploy worker - always runs ON the server. Invoked two ways, both of
# which produce an identical result:
#   * directly by deploy/deploy.sh when that is run on the server itself, and
#   * piped over SSH by deploy/deploy.sh from a dev machine.
#
# Idempotent. Configurable via env (deploy.sh sets these):
#   REMOTE_DIR  REPO_URL  BRANCH  PROXY_CONTAINER
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-/home/claude-agent/recall_select}"
REPO_URL="${REPO_URL:-git@github.com:SergeySetti/recall_select.git}"
BRANCH="${BRANCH:-master}"
PROXY_CONTAINER="${PROXY_CONTAINER:-proxy-caddy}"

if [ ! -d "$REMOTE_DIR/.git" ]; then
  echo "    First deploy - cloning $REPO_URL"
  git clone "$REPO_URL" "$REMOTE_DIR"
fi

# The repo is owned by one user (claude-agent) but may be deployed by another
# (e.g. setti) who shares its group. git refuses operations on a repo owned by a
# different uid ("dubious ownership") - register it as safe for whoever is running
# now. Idempotent: only added if not already present for this user.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$REMOTE_DIR"; then
  git config --global --add safe.directory "$REMOTE_DIR"
fi

cd "$REMOTE_DIR"

echo "    Syncing $BRANCH from origin…"
# Capture the output so we can tell *why* a fetch failed and print a targeted
# hint. The two failures we've actually hit look nothing alike: a GitHub auth
# problem (no usable SSH key) vs a local filesystem problem (repo objects owned
# by another uid, so this user can't write into .git). Misreporting the second
# as the first sends whoever's debugging down the wrong path.
if ! fetch_out=$(git fetch --all --prune 2>&1); then
  printf '%s\n' "$fetch_out"
  if printf '%s' "$fetch_out" | grep -qiE 'insufficient permission|cannot write|unable to create|failed to write object|Operation not permitted|Permission denied.*\.git'; then
    echo "!!  git fetch failed writing to the local repo at $REMOTE_DIR - a"
    echo "!!  filesystem-permission problem, NOT a GitHub auth problem. The objects"
    echo "!!  under .git are likely owned by a different user (a past deploy run as"
    echo "!!  root or another account). Fix ownership on the server, e.g.:"
    echo "!!    sudo chown -R $(whoami):$(id -gn) $REMOTE_DIR"
  else
    echo "!!  git fetch failed to reach $REPO_URL. The user running this deploy"
    echo "!!  ($(whoami)) needs GitHub pull access - i.e. an authorised SSH key in"
    echo "!!  its ~/.ssh. (claude-agent has one; a second deployer such as setti"
    echo "!!  needs their own, since SSH keys must not be shared across accounts.)"
  fi
  exit 1
fi
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "    Building and (re)starting the web container…"
docker compose build
docker compose up -d --remove-orphans

echo "    Reloading the shared Caddy proxy (picks up deploy/caddy fragments)…"
docker exec "$PROXY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
  || echo "    (proxy not running or reload failed - check the proxy stack)"

echo "    Reclaiming disk (dangling images + build cache > 1 week)…"
docker image prune -f >/dev/null
docker builder prune -f --filter "until=168h" >/dev/null

docker compose ps

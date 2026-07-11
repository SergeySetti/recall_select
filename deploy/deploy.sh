#!/usr/bin/env bash
#
# Deploy recall.select. Works from two places, no flags needed:
#
#   * From a dev machine (or the agent's box): pushes local commits, then runs the
#     deploy on the server over the `recall-server` SSH alias.
#   * On the server itself (e.g. `setti@setti-server:~/recall_select$ ./deploy/deploy.sh`):
#     deploys in place - no SSH hop.
#
# Either way the heavy lifting is deploy/_server_deploy.sh (git sync + Compose
# rebuild + Caddy reload), so both paths run identical logic. The server build is
# from source; no artifacts are copied by hand.
#
# Usage:  ./deploy/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER="$SCRIPT_DIR/_server_deploy.sh"

SSH_ALIAS="${RECALL_SSH_ALIAS:-recall-server}"
REMOTE_DIR="${RECALL_REMOTE_DIR:-/home/claude-agent/recall_select}"
REPO_URL="${RECALL_REPO_URL:-git@github.com:SergeySetti/recall_select.git}"
BRANCH="${RECALL_BRANCH:-master}"
SERVER_HOST="${RECALL_SERVER_HOST:-setti-server}"
PROXY_CONTAINER="${RECALL_PROXY_CONTAINER:-proxy-caddy}"

# Already on the server → deploy in place, skipping the push + SSH hop.
if [ "$(hostname)" = "$SERVER_HOST" ]; then
  echo "==> On $SERVER_HOST - deploying $REMOTE_DIR in place…"
  REMOTE_DIR="$REMOTE_DIR" REPO_URL="$REPO_URL" BRANCH="$BRANCH" \
    PROXY_CONTAINER="$PROXY_CONTAINER" exec bash "$WORKER"
fi

# Dev machine → ship the code, then run the worker on the server over SSH. The
# worker is piped in over stdin, so the server runs this checkout's deploy logic
# (even before it has pulled it).
echo "==> Pushing local commits ($BRANCH)…"
git push origin "$BRANCH"

echo "==> Deploying on $SSH_ALIAS:$REMOTE_DIR…"
ssh "$SSH_ALIAS" \
  REMOTE_DIR="$REMOTE_DIR" REPO_URL="$REPO_URL" BRANCH="$BRANCH" PROXY_CONTAINER="$PROXY_CONTAINER" \
  'bash -s' < "$WORKER"

echo "==> Done. Live behind the shared Caddy proxy at https://recall.select"

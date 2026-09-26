#!/usr/bin/env bash
#
# Pull-based deployer for aemet-avisos-bot.
#
# Run periodically by aemet-avisos-bot-update.timer. Fetches the deployment
# branch and, when it holds a new commit whose CI has passed, fast-forwards to
# it, reinstalls dependencies and restarts the bot.
#
# Fails closed. Any error, an unreachable GitHub API, a branch that has
# diverged locally, or any CI verdict short of an unambiguous pass leaves the
# running version untouched.
#
# Expects to run as root: it drops to the service account for every write to
# the repository, and keeps root only to restart the unit. Running it directly
# as the service account also works for a manual deploy, provided that account
# can restart the service.
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/aemet-avisos-bot}
BRANCH=${BRANCH:-main}
SERVICE=${SERVICE:-aemet-avisos-bot}
RUN_AS=${RUN_AS:-aemetbot}
API_REPO=${API_REPO:-jaimebg/aemet-avisos-bot}
# Set to 0 to deploy whatever is on the branch without consulting CI.
REQUIRE_GREEN_CI=${REQUIRE_GREEN_CI:-1}

# Records the last commit that was fully deployed: dependencies installed,
# checkout moved and service restarted. It is written only after all three
# succeed, so a run that dies half-way is retried by the next one instead of
# being mistaken for finished because HEAD already moved.
DEPLOYED_REF=refs/deploy/current

log() { printf '%s\n' "$*"; }

# Run a command as the service account, so repository files never end up owned
# by root. A no-op when we are already that account.
as_service_account() {
    if [ "$(id -un)" = "$RUN_AS" ]; then
        "$@"
    else
        runuser -u "$RUN_AS" -- "$@"
    fi
}

restart_service() {
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart "$SERVICE"
    else
        sudo -n systemctl restart "$SERVICE"
    fi
}

# Every check run recorded for a commit must have finished and passed. A commit
# with no check runs at all counts as not green: it means CI has not started
# yet, or never will, and neither is evidence the code works.
ci_is_green() {
    local sha=$1 body
    if ! body=$(curl --fail --silent --show-error --max-time 30 \
        -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/${API_REPO}/commits/${sha}/check-runs" 2>&1); then
        log "Could not read check runs from the GitHub API: ${body}"
        return 1
    fi

    printf '%s' "$body" | python3 -c '
import json
import sys

try:
    runs = json.load(sys.stdin).get("check_runs") or []
except ValueError:
    sys.exit(1)

if not runs:
    sys.exit(1)

sys.exit(0 if all(
    run.get("status") == "completed"
    and run.get("conclusion") in ("success", "skipped")
    for run in runs
) else 1)
'
}

# The last fully deployed commit. Before the first run that records one, the
# checked-out commit is the best available answer.
deployed_commit() {
    as_service_account git rev-parse --verify --quiet "${DEPLOYED_REF}^{commit}" \
        || as_service_account git rev-parse HEAD
}

# Install the dependencies of a commit that is not checked out yet. Done before
# the checkout moves, so a failed install leaves code and dependencies matching.
install_requirements() {
    local commit=$1 reqs
    reqs=$(as_service_account mktemp)
    # shellcheck disable=SC2064  # expand $reqs now; it is local to this call
    trap "rm -f '$reqs'" EXIT
    as_service_account git show "${commit}:requirements.txt" >"$reqs"
    as_service_account .venv/bin/pip install --quiet --requirement "$reqs"
    rm -f "$reqs"
    trap - EXIT
}

main() {
    cd "$REPO_DIR"

    as_service_account git fetch --quiet origin "$BRANCH"

    # Every git call goes through the service account, including the read-only
    # ones: git refuses to operate on a repository owned by another user
    # ("dubious ownership"), and this script normally runs as root.
    local current target
    current=$(deployed_commit)
    target=$(as_service_account git rev-parse "origin/${BRANCH}")

    # Nothing new. Stay silent, or the journal fills with one entry per run.
    [ "$current" = "$target" ] && exit 0

    log "origin/${BRANCH} is at ${target:0:7}; deployed version is ${current:0:7}."

    if [ "$REQUIRE_GREEN_CI" = "1" ] && ! ci_is_green "$target"; then
        log "CI for ${target:0:7} has not passed. Keeping ${current:0:7}."
        exit 0
    fi

    install_requirements "$target"
    # --ff-only so a locally diverged checkout aborts the deploy rather than
    # silently creating a merge commit on the server. Merging onto a checkout
    # an interrupted run already advanced is a harmless no-op.
    as_service_account git merge --ff-only "$target"
    restart_service
    as_service_account git update-ref "$DEPLOYED_REF" "$target"

    log "Deployed ${target:0:7}."
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration (set via environment) ─────────────────────────────
REPO_URL=${REPO_URL:-github.com/gofiber/docs.git}
AUTHOR_EMAIL=${AUTHOR_EMAIL:-github-actions[bot]@users.noreply.github.com}
AUTHOR_USERNAME=${AUTHOR_USERNAME:-github-actions[bot]}
REPO_TYPE=${REPO_TYPE:-single}
SOURCE_DIR=${SOURCE_DIR:-.}
DESTINATION_DIR=${DESTINATION_DIR:-docs}
VERSION_FILE=${VERSION_FILE:-}
DOCUSAURUS_COMMAND=${DOCUSAURUS_COMMAND:-}
FILE_PATTERNS=${FILE_PATTERNS:-*.md}
EXCLUDE_PATTERNS=${EXCLUDE_PATTERNS:-}
COMMIT_URL=${COMMIT_URL:-}
GH_REPO=${GH_REPO:-}

TOKEN=${TOKEN:?TOKEN is required}
EVENT=${EVENT:?EVENT is required}
TAG_NAME=${TAG_NAME:-}

# ─── Logging ─────────────────────────────────────────────────────────
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
trap 'log "ERROR: script failed at line ${LINENO}"' ERR

log "Starting sync_docs.sh"
log "Event: ${EVENT} | Type: ${REPO_TYPE}"
log "Source: ${SOURCE_DIR} -> Destination: ${DESTINATION_DIR}"
[[ -n "${TAG_NAME:-}" ]] && log "Tag: ${TAG_NAME}"

# ─── Git setup ───────────────────────────────────────────────────────
git config --global user.email "${AUTHOR_EMAIL}"
git config --global user.name "${AUTHOR_USERNAME}"

SOURCE_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

log "Cloning docs repo"
git clone "https://${TOKEN}@${REPO_URL}" fiber-docs

# ─── Push with retry ────────────────────────────────────────────────
push_with_retry() {
    local max_retries=5 delay=5 retry=0
    while ((retry < max_retries)); do
        git push "https://${TOKEN}@${REPO_URL}" && return 0
        retry=$((retry + 1))
        log "Push failed, retry ${retry}/${max_retries}"
        git pull --rebase
        sleep $delay
    done
    log "Failed to push after ${max_retries} attempts"
    exit 1
}

# ─── Push handler: rsync docs to docs repo ──────────────────────────
handle_push() {
    log "Syncing current docs (push mode)"
    local dest="fiber-docs/${DESTINATION_DIR}"
    mkdir -p "$dest"

    local -a args=(-av --delete --prune-empty-dirs)

    # Exclude internal directories (always)
    args+=(--exclude 'fiber-docs/***' --exclude '.*')

    # Exclude user-specified directories
    if [[ -n "$EXCLUDE_PATTERNS" ]]; then
        IFS=',' read -ra excludes <<< "$EXCLUDE_PATTERNS"
        for e in "${excludes[@]}"; do
            args+=(--exclude "${e## }/***")
        done
    fi

    # Include directory traversal
    args+=(--include '*/')

    # Include desired file patterns
    IFS=',' read -ra patterns <<< "$FILE_PATTERNS"
    for p in "${patterns[@]}"; do
        args+=(--include "${p## }")
    done

    # Exclude everything else
    args+=(--exclude '*')

    args+=("${SOURCE_DIR}/" "${dest}/")
    rsync "${args[@]}"
    log "rsync completed"
}

# ─── npm ci (run once, cached) ──────────────────────────────────────
ensure_npm_ready() {
    if [[ ! -f "fiber-docs/.npm_ready" ]]; then
        log "Running npm ci in fiber-docs"
        pushd fiber-docs >/dev/null
        npm ci
        touch .npm_ready
        popd >/dev/null
    fi
}

# ─── Release handler: Docusaurus version snapshot for one tag ───────
handle_release() {
    local tag="$1"
    if [[ -z "$tag" ]]; then
        log "ERROR: tag is required for release mode"
        exit 1
    fi

    log "Creating version snapshot for: ${tag}"

    # Compute Docusaurus version identifier
    local new_version
    if [[ "$REPO_TYPE" == "single" ]]; then
        # fiber: v3.0.0 -> v3.x
        local major="${tag%%.*}"
        major="${major#v}"
        new_version="v${major}.x"
    else
        # Multi-module: strip SOURCE_DIR prefix, then parse package/version
        local stripped="$tag"
        if [[ -n "$SOURCE_DIR" && "$SOURCE_DIR" != "." ]]; then
            stripped="${tag#"${SOURCE_DIR}/"}"
        fi
        local package_name="${stripped%/*}"
        local version_part="${stripped##*/}"
        local major="${version_part%%.*}"
        # Replace slashes in package name (e.g. testhelpers/redis → testhelpers_redis)
        local safe_package="${package_name//\//_}"
        if [[ -n "$SOURCE_DIR" && "$SOURCE_DIR" != "." ]]; then
            new_version="${SOURCE_DIR}_${safe_package}_${major}.x.x"
        else
            new_version="${safe_package}_${major}.x.x"
        fi
    fi

    log "Version identifier: ${new_version}"

    ensure_npm_ready

    pushd fiber-docs >/dev/null

    # Remove existing version entry (if any)
    if [[ -n "$VERSION_FILE" && -f "$VERSION_FILE" ]]; then
        log "Removing existing ${new_version} from ${VERSION_FILE}"
        jq --arg v "$new_version" 'del(.[] | select(. == $v))' \
            "$VERSION_FILE" > "${VERSION_FILE}.tmp"
        mv "${VERSION_FILE}.tmp" "$VERSION_FILE"
    fi

    # Run Docusaurus versioning command
    if [[ -n "$DOCUSAURUS_COMMAND" ]]; then
        log "Running: ${DOCUSAURUS_COMMAND} ${new_version}"
        ${DOCUSAURUS_COMMAND} "${new_version}"
    fi

    # Sort version file
    if [[ -n "$VERSION_FILE" && -f "$VERSION_FILE" ]]; then
        log "Sorting ${VERSION_FILE}"
        jq 'sort | reverse' "$VERSION_FILE" > "${VERSION_FILE}.tmp"
        mv "${VERSION_FILE}.tmp" "$VERSION_FILE"
    fi

    popd >/dev/null
    log "Version snapshot created: ${new_version}"
}

# ─── Release-all handler: version snapshots for all latest releases ─
handle_release_all() {
    if [[ -z "$GH_REPO" ]]; then
        log "ERROR: GH_REPO is required for release-all mode"
        exit 1
    fi

    log "Fetching releases from ${GH_REPO}"

    if [[ "$REPO_TYPE" == "single" ]]; then
        local tag
        tag=$(gh release list --repo "$GH_REPO" --exclude-drafts --exclude-pre-releases \
            --limit 1 --json tagName --jq '.[0].tagName // ""')
        if [[ -z "$tag" ]]; then
            log "No releases found"
            return
        fi
        log "Latest release: ${tag}"
        handle_release "$tag"
    else
        # Fetch releases (newest first)
        local tags
        tags=$(gh release list --repo "$GH_REPO" --exclude-drafts --exclude-pre-releases \
            --limit 200 --json tagName --jq '.[].tagName')

        # Group by module prefix, keep only the latest per module
        declare -A seen
        local count=0
        while IFS= read -r tag; do
            [[ -z "$tag" ]] && continue
            # Extract module prefix: everything before /vN.N.N
            local prefix
            prefix=$(printf '%s' "$tag" | sed -E 's|(.*)/v[0-9]+\.[0-9]+\.[0-9]+.*|\1|')
            if [[ -z "${seen[$prefix]+x}" ]]; then
                seen["$prefix"]="$tag"
                ((count++)) || true
            fi
        done <<< "$tags"

        log "Found ${count} modules with releases"

        for prefix in $(printf '%s\n' "${!seen[@]}" | sort); do
            log "Processing module '${prefix}': ${seen[$prefix]}"
            handle_release "${seen[$prefix]}"
        done
    fi
}

# ─── Main dispatch ──────────────────────────────────────────────────
case "$EVENT" in
    push)        handle_push ;;
    release)     handle_release "$TAG_NAME" ;;
    release-all) handle_release_all ;;
    *)           log "Unknown event: ${EVENT}"; exit 1 ;;
esac

# ─── Commit and push ────────────────────────────────────────────────
pushd fiber-docs >/dev/null

# Remove npm marker before committing
rm -f .npm_ready

if git status --porcelain | grep -q .; then
    log "Changes detected - committing"
    git add .

    case "$EVENT" in
        push)
            git commit -m "Add docs from ${COMMIT_URL}/commit/${SOURCE_COMMIT}"
            ;;
        release)
            encoded_tag="${TAG_NAME//\//%2F}"
            git commit -m "Sync docs for release ${COMMIT_URL}/releases/tag/${encoded_tag}"
            ;;
        release-all)
            git commit -m "Sync all latest release docs from ${COMMIT_URL}"
            ;;
    esac

    push_with_retry
    log "Push completed"
else
    log "No changes to push"
fi

popd >/dev/null
log "sync_docs.sh finished"

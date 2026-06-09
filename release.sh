#!/usr/bin/env bash
# release.sh — bump version, build amd64 Docker images, push to Hub, update flash.
#
# Usage:
#   ./release.sh patch          # 0.2.0 → 0.2.1
#   ./release.sh minor          # 0.2.0 → 0.3.0
#   ./release.sh major          # 0.2.0 → 1.0.0
#   ./release.sh 0.5.0          # set exact version
#   ./release.sh patch --no-push        # build only, skip Hub push
#   ./release.sh patch --no-flash       # skip flash drive copy
#   ./release.sh patch --dry-run        # print what would happen, do nothing
#
# Requirements:
#   - docker (with buildx multiplatform support)
#   - git (clean working tree recommended)
#   - Flash drive at /Volumes/SANDISK (optional, skip with --no-flash)

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO="artemivanchenko/printer-log-analytics"
SERVICES=(api worker scheduler telegram-bot mcp)
FLASH_MOUNT="/Volumes/SANDISK"
FLASH_PROJECT="$FLASH_MOUNT/printer-log-analytics"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Parse args ────────────────────────────────────────────────────────────────
BUMP="${1:-patch}"
PUSH=true
FLASH=true
DRY_RUN=false

for arg in "${@:2}"; do
  case "$arg" in
    --no-push)  PUSH=false ;;
    --no-flash) FLASH=false ;;
    --dry-run)  DRY_RUN=true; PUSH=false; FLASH=false ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "  [info]  $*"; }
step()  { echo; echo "▶  $*"; }
die()   { echo "✗ $*" >&2; exit 1; }
run()   { if $DRY_RUN; then echo "  [dry]   $*"; else "$@"; fi; }

bump_semver() {
  local v="$1" part="$2"
  IFS='.' read -r maj min pat <<< "$v"
  case "$part" in
    major) echo "$((maj+1)).0.0" ;;
    minor) echo "$maj.$((min+1)).0" ;;
    patch) echo "$maj.$min.$((pat+1))" ;;
    *)     echo "$part" ;;   # exact version passed
  esac
}

# ── Preflight ─────────────────────────────────────────────────────────────────
step "Preflight checks"

cd "$SCRIPT_DIR"

[[ -f VERSION ]] || die "VERSION file not found in $SCRIPT_DIR"

CURRENT=$(cat VERSION | tr -d '[:space:]')
info "Current version: $CURRENT"

if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  NEW_VERSION="$BUMP"
else
  NEW_VERSION=$(bump_semver "$CURRENT" "$BUMP")
fi

info "New version:     $NEW_VERSION"

if ! $DRY_RUN; then
  if ! git diff --quiet HEAD 2>/dev/null; then
    echo "  [warn]  Working tree is dirty — uncommitted changes will be included."
  fi
fi

GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
info "Git commit:      $GIT_COMMIT"
info "Build date:      $BUILD_DATE"

if $DRY_RUN; then
  echo
  echo "Dry run — would release $CURRENT → $NEW_VERSION"
  echo "  Services: ${SERVICES[*]}"
  echo "  Push:     $PUSH   Flash: $FLASH"
  exit 0
fi

# ── Bump version files ────────────────────────────────────────────────────────
step "Bumping version $CURRENT → $NEW_VERSION"

echo "$NEW_VERSION" > VERSION
info "Updated VERSION"

# pyproject.toml — update the version = "..." line in [project] section
if [[ "$(uname)" == "Darwin" ]]; then
  sed -i '' "s/^version = \"${CURRENT}\"/version = \"${NEW_VERSION}\"/" pyproject.toml
else
  sed -i "s/^version = \"${CURRENT}\"/version = \"${NEW_VERSION}\"/" pyproject.toml
fi
info "Updated pyproject.toml"

# ── Git commit + tag ──────────────────────────────────────────────────────────
step "Git commit and tag v$NEW_VERSION"

run git add VERSION pyproject.toml
run git commit -m "chore: release v${NEW_VERSION}

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
run git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION}"
info "Tagged v${NEW_VERSION}"

# ── Docker build ──────────────────────────────────────────────────────────────
step "Building Docker images (linux/amd64)"

# Ensure builder exists — always remove and recreate to avoid stale state
docker buildx rm pla-builder 2>/dev/null || true
docker buildx create --name pla-builder --driver docker-container --use
info "Builder pla-builder ready"

COMMON_ARGS=(
  --platform linux/amd64
  --build-arg "APP_VERSION=${NEW_VERSION}"
  --build-arg "GIT_COMMIT=${GIT_COMMIT}"
  --build-arg "BUILD_DATE=${BUILD_DATE}"
)

for svc in "${SERVICES[@]}"; do
  step "Building $svc"
  TAGS=(
    -t "${REPO}:${svc}"
    -t "${REPO}:${svc}-v${NEW_VERSION}"
  )
  if $PUSH; then
    run docker buildx build \
      "${COMMON_ARGS[@]}" \
      "${TAGS[@]}" \
      -f "Dockerfile.${svc}" \
      --push \
      .
    info "Pushed ${REPO}:${svc} and ${REPO}:${svc}-v${NEW_VERSION}"
  else
    run docker buildx build \
      "${COMMON_ARGS[@]}" \
      "${TAGS[@]}" \
      -f "Dockerfile.${svc}" \
      --load \
      .
    info "Built ${REPO}:${svc} (local only)"
  fi
done

# ── Flash drive update ─────────────────────────────────────────────────────────
if $FLASH; then
  step "Updating flash drive at $FLASH_MOUNT"

  if [[ ! -d "$FLASH_MOUNT" ]]; then
    echo "  [warn]  Flash drive not mounted at $FLASH_MOUNT — skipping."
  else
    # Sync source code (exclude heavy artifacts)
    run rsync -a --delete \
      --exclude='.git' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.env' \
      --exclude='*.tar' \
      --exclude='*.tar.gz' \
      "${SCRIPT_DIR}/" \
      "${FLASH_PROJECT}/"
    info "Synced source to flash"

    if ! $PUSH; then
      # Save images as tar (offline install)
      TAR_PATH="${FLASH_MOUNT}/pla-images-amd64.tar"
      info "Saving images to $TAR_PATH ..."
      IMAGE_LIST=()
      for svc in "${SERVICES[@]}"; do
        IMAGE_LIST+=("${REPO}:${svc}")
      done
      run docker save "${IMAGE_LIST[@]}" -o "$TAR_PATH"
      info "Saved $(du -sh "$TAR_PATH" | cut -f1) image archive"
    fi

    info "Flash drive updated ✓"
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
step "Release v${NEW_VERSION} complete"
echo
echo "  Version:    $NEW_VERSION"
echo "  Git tag:    v${NEW_VERSION} (run: git push && git push --tags)"
if $PUSH; then
  echo "  Docker Hub: ${REPO}:api  (and :api-v${NEW_VERSION}, :worker-v${NEW_VERSION}, …)"
  echo
  echo "  To deploy on the printer machine:"
  echo "    docker compose pull && docker compose up -d"
fi

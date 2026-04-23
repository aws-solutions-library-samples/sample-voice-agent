#!/bin/bash
# build-base-image.sh — build & push the voice-agent pre-baked base image.
#
# The base image is tagged with sha256(requirements.txt). If a base image
# with the current hash already exists in ECR, this script is a no-op (cheap
# check). Otherwise it builds, pushes, and prints the new URI.
#
# Usage:
#   ./scripts/build-base-image.sh              # build + push if needed, pretty output
#   ./scripts/build-base-image.sh --uri-only   # silent unless error; echo URI only
#   ./scripts/build-base-image.sh --force      # rebuild even if hash exists
#
# Used by deploy.sh to compute the BASE_IMAGE build arg before `cdk deploy`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_PROFILE="${AWS_PROFILE:-default}"
REPO_NAME="${BASE_IMAGE_REPO_NAME:-voice-agent-base}"
VOICE_AGENT_DIR="$REPO_ROOT/backend/voice-agent"
BASE_DOCKERFILE="$VOICE_AGENT_DIR/Dockerfile.base"
REQUIREMENTS_FILE="$VOICE_AGENT_DIR/requirements.txt"

# Flags
URI_ONLY=false
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --uri-only) URI_ONLY=true ;;
        --force)    FORCE=true ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# Only print chatter if not in --uri-only mode.
log()  { if ! $URI_ONLY; then echo "$@"; fi }
warn() { echo "$@" >&2; }

if [ ! -f "$REQUIREMENTS_FILE" ]; then
    warn "requirements.txt not found at $REQUIREMENTS_FILE"
    exit 1
fi
if [ ! -f "$BASE_DOCKERFILE" ]; then
    warn "Dockerfile.base not found at $BASE_DOCKERFILE"
    exit 1
fi

# Compute a deterministic tag from the content that affects the base image.
# Include Dockerfile.base itself so base-image-only tweaks (e.g. bumping the
# python version) force a rebuild even if requirements.txt is unchanged.
HASH_INPUT=$(cat "$REQUIREMENTS_FILE" "$BASE_DOCKERFILE")
REQ_HASH=$(printf '%s' "$HASH_INPUT" | shasum -a 256 | awk '{print substr($1, 1, 16)}')
TAG="req-$REQ_HASH"

# Resolve AWS account for ECR URI.
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")
ECR_REGISTRY="$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE_URI="$ECR_REGISTRY/$REPO_NAME:$TAG"

log "Base image plan:"
log "  Hash input:   requirements.txt + Dockerfile.base"
log "  Tag:          $TAG"
log "  Target:       $IMAGE_URI"
log ""

# Ensure the ECR repo exists. Safe to call — creates once, then 'already
# exists' is swallowed.
if ! aws ecr describe-repositories \
        --repository-names "$REPO_NAME" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        >/dev/null 2>&1; then
    log "  ECR repo '$REPO_NAME' does not exist — creating..."
    aws ecr create-repository \
        --repository-name "$REPO_NAME" \
        --image-scanning-configuration scanOnPush=true \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        >/dev/null
    log "  Created repository."
fi

# Check if this tag already exists — skip the build if so.
if ! $FORCE && aws ecr describe-images \
        --repository-name "$REPO_NAME" \
        --image-ids "imageTag=$TAG" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        >/dev/null 2>&1; then
    log "  Tag '$TAG' already exists in ECR — skipping build."
    if $URI_ONLY; then echo "$IMAGE_URI"; fi
    exit 0
fi

log "  Tag '$TAG' is new — building..."
log ""

# Log docker into ECR so we can push.
aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null

# Build the base image. Explicit --platform so ARM dev machines still produce
# linux/amd64 for Fargate.
docker build \
    --platform linux/amd64 \
    -f "$BASE_DOCKERFILE" \
    -t "$IMAGE_URI" \
    "$VOICE_AGENT_DIR" \
    >&2

log ""
log "  Pushing..."
docker push "$IMAGE_URI" >&2

log ""
log "  ✓ Pushed $IMAGE_URI"

if $URI_ONLY; then
    echo "$IMAGE_URI"
fi

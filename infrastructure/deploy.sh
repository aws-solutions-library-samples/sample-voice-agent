#!/bin/bash
# infrastructure/deploy.sh
# Comprehensive deployment script for Voice Agent POC

set -e  # Exit on error

# Use finch as docker alternative if docker not available
if ! command -v docker &>/dev/null && command -v finch &>/dev/null; then
    export CDK_DOCKER=finch
fi

# ===================================
# Color Output
# ===================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ===================================
# Helper Functions
# ===================================
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# ===================================
# Prerequisites
# ===================================
check_prerequisites() {
    print_status "Checking prerequisites..."

    if ! command_exists node; then
        print_error "Node.js is not installed"
        print_error "Install from: https://nodejs.org/"
        exit 1
    fi

    local node_version=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
    if [ "$node_version" -lt 18 ]; then
        print_error "Node.js version 18 or higher required (found: $(node -v))"
        exit 1
    fi

    if ! command_exists npm; then
        print_error "npm is not installed"
        exit 1
    fi

    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        print_error "AWS credentials not configured"
        print_error "Run 'aws configure' or set AWS_PROFILE"
        exit 1
    fi

    print_success "All prerequisites met"
}

# ===================================
# Environment Loading
# ===================================
load_env() {
    if [ -f .env ]; then
        print_status "Loading environment from .env"
        export $(grep -v '^#' .env | xargs)
    else
        print_warning ".env file not found. Using defaults from cdk.json context."
        print_warning "Copy .env.example to .env for custom configuration."
    fi
}

# ===================================
# Validation
# ===================================
validate_env() {
    print_status "Validating environment..."

    # Get AWS account from credentials
    local aws_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
    if [ -z "$aws_account" ]; then
        print_error "Could not determine AWS account ID"
        exit 1
    fi
    export AWS_ACCOUNT_ID="$aws_account"

    # Get region from env or default
    if [ -z "$AWS_REGION" ]; then
        export AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
    fi

    print_success "Environment validated"
    print_status "  Account: $AWS_ACCOUNT_ID"
    print_status "  Region:  $AWS_REGION"
    print_status "  Environment: ${ENVIRONMENT:-poc}"
}

# ===================================
# CDK Bootstrap
# ===================================
bootstrap_cdk() {
    print_status "Checking CDK bootstrap..."

    if ! aws cloudformation describe-stacks \
        --stack-name CDKToolkit \
        --region "$AWS_REGION" >/dev/null 2>&1; then

        print_warning "Bootstrapping CDK..."
        npx cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}"
    else
        print_success "CDK already bootstrapped"
    fi
}

# ===================================
# Install Dependencies
# ===================================
install_deps() {
    print_status "Installing dependencies..."
    npm install
    print_success "Dependencies installed"
}

# ===================================
# Build
# ===================================
build() {
    print_status "Compiling TypeScript..."
    npm run build
    print_success "Build complete"
}

# ===================================
# Synthesize
# ===================================
synth() {
    print_status "Synthesizing CloudFormation templates..."
    npx cdk synth
    print_success "Synthesis complete"
}

# ===================================
# Pre-baked base image
# ===================================
# Build & push the voice-agent base image if its content hash changed. The
# main Dockerfile's FROM line points at voice-agent-base:req-<hash>; if the
# tag already exists in ECR, this is a no-op (~2s). If requirements.txt or
# Dockerfile.base changed, we rebuild (~8 min) and push, THEN let CDK build
# the thin app layer on top (~30s).
ensure_base_image() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local build_script="$script_dir/scripts/build-base-image.sh"

    if [ ! -x "$build_script" ]; then
        print_error "Base image builder not found or not executable: $build_script"
        exit 1
    fi

    print_status "Ensuring voice-agent base image is present in ECR..."
    local base_uri
    base_uri=$("$build_script" --uri-only)
    export VOICE_AGENT_BASE_IMAGE="$base_uri"
    print_success "Base image: $VOICE_AGENT_BASE_IMAGE"

    # CDK's docker build needs ECR login to pull the base image referenced
    # in the app Dockerfile's FROM line. build-base-image.sh also runs this
    # but only if it actually builds — we need the login here regardless.
    local account region registry
    account=$(aws sts get-caller-identity --query Account --output text)
    region="${AWS_REGION:-us-east-1}"
    registry="$account.dkr.ecr.$region.amazonaws.com"
    aws ecr get-login-password --region "$region" \
        | docker login --username AWS --password-stdin "$registry" >/dev/null 2>&1 \
        || print_warning "ECR login failed — CDK docker build may fail to pull base image"
}

# ===================================
# Deploy
# ===================================
deploy() {
    print_status "Deploying stacks..."

    local require_approval="never"
    if [ "${REQUIRE_APPROVAL:-false}" = "true" ]; then
        require_approval="broadening"
    fi

    # Build/push the voice-agent base image if requirements.txt or
    # Dockerfile.base changed. Exports VOICE_AGENT_BASE_IMAGE for CDK.
    ensure_base_image

    # Deploy stacks in dependency order
    # SSM parameters are used for cross-stack communication, so each stack
    # must be deployed before stacks that depend on its SSM parameters

    # Phase 1: Network (no dependencies)
    print_status "Phase 1: Deploying Network stack..."
    npx cdk deploy VoiceAgentNetwork \
        --require-approval "$require_approval" \
        --outputs-file outputs-network.json
    print_success "Network stack deployed"

    # Phase 2: Storage (depends on Network for VPC endpoints)
    print_status "Phase 2: Deploying Storage stack..."
    npx cdk deploy VoiceAgentStorage \
        --require-approval "$require_approval" \
        --outputs-file outputs-storage.json
    print_success "Storage stack deployed"

    # Phase 3: SageMaker (depends on Network)
    print_status "Phase 3: Deploying SageMaker stack..."
    npx cdk deploy VoiceAgentSageMaker \
        --require-approval "$require_approval" \
        --outputs-file outputs-sagemaker.json
    print_success "SageMaker stack deployed"

    # Phase 4: ECS (depends on Network, Storage)
    print_status "Phase 4: Deploying ECS stack..."
    npx cdk deploy VoiceAgentEcs \
        --require-approval "$require_approval" \
        --outputs-file outputs-ecs.json
    print_success "ECS stack deployed"

    # Phase 5: BotRunner (depends on all previous stacks)
    print_status "Phase 5: Deploying BotRunner stack..."
    npx cdk deploy VoiceAgentBotRunner \
        --require-approval "$require_approval" \
        --outputs-file outputs-botrunner.json
    print_success "BotRunner stack deployed"

    # Merge all outputs
    print_status "Merging stack outputs..."
    echo "{}" > outputs.json
    for f in outputs-network.json outputs-storage.json outputs-sagemaker.json outputs-ecs.json outputs-botrunner.json; do
        if [ -f "$f" ]; then
            jq -s '.[0] * .[1]' outputs.json "$f" > outputs-merged.json && mv outputs-merged.json outputs.json
            rm -f "$f"
        fi
    done

    print_success "Deployment complete!"
    print_status "Stack outputs saved to outputs.json"
}

# ===================================
# Deploy Single Stack
# ===================================
deploy_stack() {
    local stack_name="$1"
    print_status "Deploying stack: $stack_name..."

    # ECS stack is the only one that consumes the voice-agent image — skip
    # the base-image build for other stacks so Network/Storage deploys
    # don't pay the ECR check round-trip.
    if [ "$stack_name" = "VoiceAgentEcs" ]; then
        ensure_base_image
    fi

    npx cdk deploy "$stack_name" \
        --require-approval never \
        --outputs-file "outputs-${stack_name}.json"

    print_success "Stack $stack_name deployed"
}

# ===================================
# Diff
# ===================================
diff() {
    print_status "Checking for changes..."
    npx cdk diff --all
}

# ===================================
# Run Tests Before Deploy
# ===================================
run_pre_deploy_tests() {
    print_status "Running pre-deployment tests..."

    # Run TypeScript tests
    if npm test; then
        print_success "Unit tests passed"
    else
        print_error "Unit tests failed"
        exit 1
    fi

    # Run linting
    if npm run lint; then
        print_success "Linting passed"
    else
        print_error "Linting failed"
        exit 1
    fi
}

# ===================================
# Run Integration Tests
# ===================================
run_integration_tests() {
    print_status "Running integration tests..."

    if [ -f scripts/test-integration.sh ]; then
        chmod +x scripts/test-integration.sh
        if ./scripts/test-integration.sh; then
            print_success "Integration tests passed"
        else
            print_warning "Some integration tests failed - review output above"
        fi
    else
        print_warning "Integration test script not found"
    fi
}

# ===================================
# Test Webhook Endpoint
# ===================================
test_webhook() {
    print_status "Testing webhook endpoint..."

    if [ -f scripts/test-webhook.sh ]; then
        chmod +x scripts/test-webhook.sh
        ./scripts/test-webhook.sh "$1"
    else
        print_warning "Webhook test script not found"
    fi
}

# ===================================
# Destroy
# ===================================
destroy() {
    print_warning "This will destroy all stacks!"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_status "Destroying stacks..."
        npx cdk destroy --all --force
        print_success "All stacks destroyed"
    else
        print_status "Destroy cancelled"
    fi
}

# ===================================
# Usage
# ===================================
usage() {
    echo "Usage: $0 {deploy|deploy-stack|diff|synth|test|verify|webhook|destroy|bootstrap}"
    echo ""
    echo "Commands:"
    echo "  deploy        Deploy all stacks (runs tests first)"
    echo "  deploy-stack  Deploy a single stack (usage: $0 deploy-stack StackName)"
    echo "  diff          Show pending changes"
    echo "  synth         Synthesize CloudFormation templates"
    echo "  test          Run unit tests and linting"
    echo "  verify        Run integration tests against deployed infrastructure"
    echo "  webhook       Test webhook endpoint (usage: $0 webhook [url])"
    echo "  destroy       Destroy all stacks"
    echo "  bootstrap     Bootstrap CDK in the AWS account"
    echo ""
    echo "Environment variables (from .env):"
    echo "  ENVIRONMENT       Deployment environment (poc, dev, staging, prod)"
    echo "  AWS_REGION        AWS region"
    echo "  REQUIRE_APPROVAL  Require approval for deployments (true/false)"
    echo "  SKIP_TESTS        Skip pre-deployment tests (true/false)"
    exit 1
}

# ===================================
# Main
# ===================================
main() {
    local command="${1:-deploy}"

    print_status "Voice Agent POC - CDK Deployment"
    print_status "================================"

    load_env
    check_prerequisites
    validate_env

    case "$command" in
        deploy)
            install_deps
            build
            if [ "${SKIP_TESTS:-false}" != "true" ]; then
                run_pre_deploy_tests
            fi
            bootstrap_cdk
            deploy
            print_status ""
            print_status "Running post-deployment verification..."
            run_integration_tests
            ;;
        deploy-stack)
            if [ -z "$2" ]; then
                print_error "Stack name required"
                echo "Usage: $0 deploy-stack StackName"
                exit 1
            fi
            install_deps
            build
            bootstrap_cdk
            deploy_stack "$2"
            ;;
        diff)
            install_deps
            build
            diff
            ;;
        synth)
            install_deps
            build
            synth
            ;;
        test)
            install_deps
            build
            run_pre_deploy_tests
            ;;
        verify)
            run_integration_tests
            ;;
        webhook)
            test_webhook "$2"
            ;;
        destroy)
            destroy
            ;;
        bootstrap)
            bootstrap_cdk
            ;;
        *)
            usage
            ;;
    esac
}

main "$@"

#!/bin/bash
# DatIA Voice — Backend Deployment Script
# Builds the container image with Podman, pushes to ECR, and deploys ECS via CloudFormation.
#
# Usage:
#   chmod +x commands/deploy_backend.sh
#   ./commands/deploy_backend.sh

set -e

# ─── CONFIG ───
PROFILE="alx-dev"
ACCOUNT_ID="011528283701"
REGION="us-east-1"
REPO_NAME="datia-voice"
IMAGE_TAG="latest"
STACK_NAME="datia-voice-stack-dev"
TEMPLATE_FILE="template.yaml"
STAGE="dev"
PROJECT_PREFIX="datia-voice"

# Auto-detect container engine: prefer podman, fallback to docker
if command -v podman &> /dev/null; then
    ENGINE="podman"
elif command -v docker &> /dev/null; then
    ENGINE="docker"
else
    echo "❌ Neither podman nor docker found. Install one of them."
    exit 1
fi
echo "Using container engine: $ENGINE"

IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "═══════════════════════════════════════════"
echo "  DatIA Voice — Backend Deploy"
echo "  Account : ${ACCOUNT_ID}"
echo "  Region  : ${REGION}"
echo "  Image   : ${IMAGE_URI}"
echo "═══════════════════════════════════════════"

# ─── STEP 1: Create ECR repository if it doesn't exist ───
echo ""
echo "📦 Step 1: Ensuring ECR repository exists..."
aws ecr describe-repositories \
    --repository-names ${REPO_NAME} \
    --region ${REGION} \
    --profile ${PROFILE} > /dev/null 2>&1 || \
aws ecr create-repository \
    --repository-name ${REPO_NAME} \
    --region ${REGION} \
    --profile ${PROFILE}
echo "✅ ECR repository ready: ${REPO_NAME}"

# ─── STEP 2: Authenticate Podman with ECR ───
echo ""
echo "🔐 Step 2: Authenticating Podman with ECR..."
aws ecr get-login-password \
    --region ${REGION} \
    --profile ${PROFILE} | \
$ENGINE login \
    --username AWS \
    --password-stdin \
    ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
echo "✅ $ENGINE authenticated with ECR"

# ─── STEP 3: Build image ───
echo ""
echo "🔨 Step 3: Building container image with $ENGINE..."
$ENGINE build -t ${REPO_NAME}:${IMAGE_TAG} .
echo "✅ Image built: ${REPO_NAME}:${IMAGE_TAG}"

# ─── STEP 4: Tag and push to ECR ───
echo ""
echo "🚀 Step 4: Pushing image to ECR..."
$ENGINE tag ${REPO_NAME}:${IMAGE_TAG} ${IMAGE_URI}
$ENGINE push ${IMAGE_URI}
echo "✅ Image pushed: ${IMAGE_URI}"

# ─── STEP 5: Deploy CloudFormation stack ───
echo ""
echo "☁️  Step 5: Deploying CloudFormation stack..."
aws cloudformation deploy \
    --template-file ${TEMPLATE_FILE} \
    --stack-name ${STACK_NAME} \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --parameter-overrides \
        Stage=${STAGE} \
        ProjectPrefix=${PROJECT_PREFIX} \
        ImageUri=${IMAGE_URI} \
    --profile ${PROFILE} \
    --region ${REGION}
echo "✅ CloudFormation stack deployed"

# ─── STEP 6: Show outputs ───
echo ""
echo "📋 Stack outputs:"
aws cloudformation describe-stacks \
    --stack-name ${STACK_NAME} \
    --query 'Stacks[0].Outputs' \
    --output table \
    --profile ${PROFILE} \
    --region ${REGION}

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Backend deployment complete!"
echo ""
echo "  NEXT STEP: Copy the WebSocketURL from the"
echo "  outputs above and update WS_URL in:"
echo "  frontend/index.html"
echo "═══════════════════════════════════════════"

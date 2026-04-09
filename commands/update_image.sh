#!/bin/bash
# DatIA Voice — Update Image Script
# Rebuilds and pushes a new image, then forces ECS to redeploy the service.
# Use this when you change server.py or nova_sonic_client.py without changing infrastructure.
#
# Usage:
#   chmod +x commands/update_image.sh
#   ./commands/update_image.sh

set -e

PROFILE="alx-dev"
ACCOUNT_ID="011528283701"
REGION="us-east-1"
REPO_NAME="datia-voice"
IMAGE_TAG="latest"
CLUSTER="datia-voice-cluster-dev"
SERVICE="datia-voice-service-dev"

# Auto-detect container engine
if command -v podman &> /dev/null; then
    ENGINE="podman"
elif command -v docker &> /dev/null; then
    ENGINE="docker"
else
    echo "❌ Neither podman nor docker found."
    exit 1
fi

IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "🔨 Building new image with $ENGINE..."
$ENGINE build -t ${REPO_NAME}:${IMAGE_TAG} .

echo "🔐 Authenticating with ECR..."
aws ecr get-login-password --region ${REGION} --profile ${PROFILE} | \
    $ENGINE login --username AWS --password-stdin \
    ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

echo "🚀 Pushing image..."
$ENGINE tag ${REPO_NAME}:${IMAGE_TAG} ${IMAGE_URI}
$ENGINE push ${IMAGE_URI}

echo "🔄 Forcing ECS service redeployment..."
aws ecs update-service \
    --cluster ${CLUSTER} \
    --service ${SERVICE} \
    --force-new-deployment \
    --profile ${PROFILE} \
    --region ${REGION} \
    --query 'service.deployments[0].{status:status,desired:desiredCount,running:runningCount}' \
    --output table

echo "✅ New image deployed. ECS will pull and restart the task in ~1-2 minutes."

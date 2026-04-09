#!/bin/bash
# DatIA Voice — Frontend Deployment Script
# Syncs frontend/index.html to S3 for public hosting.
#
# Run AFTER deploy_backend.sh and after updating WS_URL in frontend/index.html.
#
# Usage:
#   chmod +x commands/deploy_frontend.sh
#   ./commands/deploy_frontend.sh

set -e

# ─── CONFIG ───
PROFILE="alx-dev"
REGION="us-east-1"
STAGE="dev"
PROJECT_PREFIX="datia-voice"
BUCKET="${PROJECT_PREFIX}-frontend-${STAGE}"
STACK_NAME="datia-voice-stack-dev"

echo "═══════════════════════════════════════════"
echo "  DatIA Voice — Frontend Deploy"
echo "  Bucket : s3://${BUCKET}"
echo "═══════════════════════════════════════════"

# ─── Verify WS_URL is not localhost ───
if grep -q "ws://localhost" frontend/index.html; then
    echo ""
    echo "⚠️  WARNING: frontend/index.html still has WS_URL pointing to localhost!"
    echo ""
    echo "   Get the WebSocket URL from the backend stack:"
    echo "   aws cloudformation describe-stacks \\"
    echo "     --stack-name ${STACK_NAME} \\"
    echo "     --query 'Stacks[0].Outputs[?OutputKey==\`WebSocketURL\`].OutputValue' \\"
    echo "     --output text \\"
    echo "     --profile ${PROFILE}"
    echo ""
    echo "   Then update WS_URL in frontend/index.html before deploying."
    echo ""
    read -p "   Continue anyway? (y/N): " confirm
    if [ "$confirm" != "y" ]; then
        echo "Aborted."
        exit 1
    fi
fi

# ─── Sync to S3 ───
echo ""
echo "🌐 Uploading frontend to S3..."
aws s3 sync frontend/ s3://${BUCKET}/ \
    --profile ${PROFILE} \
    --region ${REGION} \
    --delete

# ─── Invalidate CloudFront cache ───
echo ""
echo "🔄 Invalidating CloudFront cache..."
CF_DIST_ID=$(aws cloudformation describe-stacks \
    --stack-name ${STACK_NAME} \
    --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontDistributionId`].OutputValue' \
    --output text \
    --profile ${PROFILE} \
    --region ${REGION} 2>/dev/null)

if [ -n "$CF_DIST_ID" ] && [ "$CF_DIST_ID" != "None" ]; then
    aws cloudfront create-invalidation \
        --distribution-id ${CF_DIST_ID} \
        --paths "/*" \
        --profile ${PROFILE} > /dev/null
    echo "✅ CloudFront cache invalidated"
else
    echo "⚠️  CloudFront distribution not found, skipping invalidation"
fi

# ─── Show URL ───
echo ""
FRONTEND_URL=$(aws cloudformation describe-stacks \
    --stack-name ${STACK_NAME} \
    --query 'Stacks[0].Outputs[?OutputKey==`FrontendURL`].OutputValue' \
    --output text \
    --profile ${PROFILE} \
    --region ${REGION} 2>/dev/null || echo "Check CloudFormation outputs")

echo "═══════════════════════════════════════════"
echo "  ✅ Frontend deployed!"
echo ""
echo "  🔗 URL: ${FRONTEND_URL}"
echo "═══════════════════════════════════════════"

#!/bin/sh
# Deploy the web app to Embarko (https://embarko.ai/docs/deploy).
#   APP_NAME   Embarko app name (default: ledger-recon)
#   DEPLOY_TOKEN  Embarko deploy token (request one at https://ship.embarko.ai/api/public/deploy-tokens/request)
# Secrets are NOT uploaded. Set them once in Embarko -> App setup (or the env-vars API):
#   SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, ANTHROPIC_API_KEY
set -eu
cd "$(dirname "$0")/.."
APP_NAME="${APP_NAME:-ledger-recon}"
: "${DEPLOY_TOKEN:?set DEPLOY_TOKEN}"
TAR="$(mktemp -t recon-deploy).tar.gz"
tar -czf "$TAR" --exclude='__pycache__' --exclude='*.pyc' main.py requirements.txt app recon
echo "Package: $(du -h "$TAR" | cut -f1)"
curl -sS -X POST 'https://ship.embarko.ai/apps' \
  -H "X-App-Name: $APP_NAME" -H 'X-Agent-Name: recon-deploy' \
  --oauth2-bearer "$DEPLOY_TOKEN" -F "source=@$TAR"
echo
echo "Status: curl -sS --oauth2-bearer \$DEPLOY_TOKEN https://ship.embarko.ai/apps/$APP_NAME/status"
echo "URL:    https://$APP_NAME.embarko.app"
rm -f "$TAR"

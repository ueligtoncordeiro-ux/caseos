#!/usr/bin/env bash
# set_github_secrets.sh — lê backend/.env e sobe cada variável como GitHub Secret
# Uso: bash scripts/set_github_secrets.sh
# Pré-requisito: gh auth login

set -e
REPO="ueligtoncordeiro-ux/caseos"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ $ENV_FILE não encontrado."; exit 1
fi

echo "🔐 Subindo secrets para $REPO ..."
while IFS='=' read -r key value; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  value=$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr -d '"'"'" )
  if [ -n "$value" ]; then
    echo "  ✅ $key"
    echo "$value" | gh secret set "$key" --repo "$REPO"
  else
    echo "  ⚠️  $key (vazio — pulado)"
  fi
done < "$ENV_FILE"

echo ""
echo "🎉 Feito! https://github.com/$REPO/settings/secrets/actions"

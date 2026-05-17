#!/bin/bash
# Inicialização local para desenvolvimento

set -e

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BACKEND_DIR"

# Verificar .env
if [ ! -f ".env" ]; then
  echo "⚠  .env não encontrado. Copiando .env.example..."
  cp .env.example .env
  echo "✏  Preencha ANTHROPIC_API_KEY em .env antes de continuar."
  exit 1
fi

# Verificar virtualenv
if [ ! -d ".venv" ]; then
  echo "🐍 Criando ambiente virtual..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "📦 Instalando dependências..."
pip install -q -r requirements.txt

echo "📁 Criando diretório de saída DOCX..."
mkdir -p docx_output

echo "🚀 Iniciando servidor RCCS em http://localhost:8000"
echo "   Docs: http://localhost:8000/docs"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000 --log-level info

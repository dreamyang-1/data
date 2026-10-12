#!/usr/bin/env bash
# MCP 工具启动脚本
# 用法: ./start.sh <tool-name>          # stdio 模式（默认）
#       ./start.sh <tool-name> http 8109  # streamable-http 模式
#
# 例：./start.sh office-mcp
#     ./start.sh office-mcp http 8109

set -euo pipefail

TOOL="${1:-}"
MODE="${2:-stdio}"
PORT="${3:-8109}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PIP_PATH="$SCRIPT_DIR/pip"
WRAPPERS_DIR="$SCRIPT_DIR/wrappers"
SOURCE_DIR="$SCRIPT_DIR/source"

export PYTHONPATH="$PIP_PATH${PYTHONPATH:+:$PYTHONPATH}"

case "$TOOL" in
  office-mcp)
    if [[ "$MODE" == "http" ]]; then
      exec python "$WRAPPERS_DIR/office_mcp.py" --transport streamable-http --port "$PORT"
    else
      exec python "$WRAPPERS_DIR/office_mcp.py"
    fi
    ;;

  imagesorcery-mcp)
    if [[ "$MODE" == "http" ]]; then
      exec python -m imagesorcery_mcp --transport streamable-http --port "$PORT"
    else
      exec python -m imagesorcery_mcp
    fi
    ;;

  mcp-email)
    if [[ "$MODE" == "http" ]]; then
      exec python "$PIP_PATH/mcp_email.py" --transport streamable-http --port "$PORT"
    else
      exec python "$PIP_PATH/mcp_email.py"
    fi
    ;;

  mcp-run-python|run-python)
    # 替代空壳包 mcp-run-isolated-python，用 deno+pyodide 真正执行 Python 沙箱
    if [[ "$MODE" == "http" ]]; then
      exec python -m mcp_run_python streamable-http --port "$PORT" --disable-networking
    else
      exec python -m mcp_run_python stdio --disable-networking
    fi
    ;;

  mcp-run-isolated-python)
    echo "ERROR: mcp-run-isolated-python 是空壳包（作者只占名无功能）" >&2
    echo "建议替代：用 ./start.sh mcp-run-python" >&2
    exit 1
    ;;

  mcp-image-optimizer|playwright-mcp|ui-mcp|uiforge-mcp|email-mcp-server|image-processing-mcp-server)
    case "$TOOL" in
      mcp-image-optimizer)      PKG="mcp-image-optimizer" ;;
      playwright-mcp)           PKG="@executeautomation/playwright-mcp-server" ;;
      ui-mcp)                   PKG="@forgespace/ui-mcp" ;;
      uiforge-mcp)              PKG="uiforge-mcp" ;;
      email-mcp-server)         PKG="email-mcp-server" ;;
      image-processing-mcp-server) PKG="image-processing-mcp-server" ;;
    esac
    exec npx -y "$PKG"
    ;;

  email-mcp-diskd)
    exec node /root/yyy/mcp-tools/source/email-mcp-diskd/dist/main.js stdio
    ;;

  onlyoffice-mcp)
    export PYTHONPATH="/root/yyy/mcp-tools/pip:/root/yyy/mcp-tools/source/onlyoffice-mcp-server/src${PYTHONPATH:+:$PYTHONPATH}"
    exec python -m onlyoffice_mcp
    ;;

  *)
    echo "Unknown tool: $TOOL" >&2
    echo "Available: office-mcp onlyoffice-mcp imagesorcery-mcp mcp-email mcp-run-isolated-python" >&2
    echo "            mcp-image-optimizer image-processing-mcp-server playwright-mcp ui-mcp uiforge-mcp email-mcp-server email-mcp-diskd" >&2
    exit 1
    ;;
esac

#!/bin/bash
# Switch Vector's brain between saved env profiles on the Pi:
#   ~/wire-pod/use-llm.sh claude     # Claude via the Anthropic API (.env.claude)
#   ~/wire-pod/use-llm.sh qwen       # Qwen on Alibaba Model Studio (.env.qwen, archived)
# Profiles hold secrets and live only on the Pi (never in git). compose reads .env, so the
# chosen profile is copied there and vector-agent is recreated with it.
set -euo pipefail
cd "$(dirname "$0")"
[ $# -eq 1 ] || { echo "usage: $0 <profile>   (profiles: $(ls .env.* 2>/dev/null | sed 's/^\.env\.//' | tr '\n' ' '))"; exit 1; }
p=".env.$1"
[ -f "$p" ] || { echo "no profile $p (have: $(ls .env.* 2>/dev/null | sed 's/^\.env\.//' | tr '\n' ' '))"; exit 1; }
if grep -q '^LLM_PROVIDER=anthropic' "$p" && ! grep -qE '^ANTHROPIC_API_KEY=.+' "$p"; then
  echo "$p has no ANTHROPIC_API_KEY yet: add it (nano $PWD/$p), then run this again"; exit 1
fi
cp -p "$p" .env
chmod 600 .env
echo "using $p"
sudo -n docker compose up -d --force-recreate vector-agent

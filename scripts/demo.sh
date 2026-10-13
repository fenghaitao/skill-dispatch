#!/usr/bin/env bash
# Copy the workspaces into a scratch folder as independent git repositories
# (the "sub-repos"), then dispatch one task to each with the chosen backend.
#
#   scripts/demo.sh [local|orca|print] [scratch-dir]
#
# The orchestrator only reads skill frontmatter; each worker runs in its own
# sub-repo and finds the skill there. `verify` then checks the receipts.
set -euo pipefail
backend="${1:-print}"
scratch="${2:-$(mktemp -d /tmp/skill-dispatch-demo.XXXXXX)}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-python3}"
model="${MODEL:-}"

mkdir -p "$scratch"
for src in "$here"/workspaces/*/; do
    name="$(basename "$src")"
    dest="$scratch/$name"
    rm -rf "$dest"
    cp -r "$src" "$dest"
    git -C "$dest" init -q -b main
    git -C "$dest" add -A
    git -C "$dest" -c user.name=demo -c user.email=demo@example.invalid commit -q -m "Initial $name"
done
echo "sub-repos in $scratch"

tasks=(
    "Add a multiply(a, b) function to calc.py"
    "Write a release note for version 0.2.0: added a multiply function"
    "Summarize sales.csv into a report"
)
for task in "${tasks[@]}"; do
    args=(dispatch --root "$scratch" --task "$task" --backend "$backend")
    [ -n "$model" ] && args+=(--model "$model")
    [ "$backend" = orca ] && args+=(--wait 600)
    "$python" "$here/orchestrator/skill_dispatch.py" "${args[@]}"
done
"$python" "$here/orchestrator/skill_dispatch.py" verify --root "$scratch"

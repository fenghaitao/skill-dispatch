#!/usr/bin/env bash
# Copy workspaces into fresh scratch repositories and dispatch one task to each.
# Usage: [AGENT=claude|copilot] [MODEL=M] scripts/demo.sh [local|orca|herdr|print]
#                       [new-scratch-dir] [--accept-edits] [--allow-tool RULE ...]
set -euo pipefail
backend="${1:-print}"
case "$backend" in
    local|orca|herdr|print) ;;
    *) echo "invalid backend: $backend" >&2; exit 2 ;;
esac
agent="${AGENT:-claude}"
case "$agent" in
    claude|copilot) ;;
    *) echo "invalid AGENT: $agent" >&2; exit 2 ;;
esac
if [ "$#" -gt 0 ]; then shift; fi
requested_scratch="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-python3}"
model="${MODEL:-}"
dispatch_options=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --accept-edits) dispatch_options+=("$1"); shift ;;
        --allow-tool)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "--allow-tool requires a rule" >&2; exit 2
            fi
            dispatch_options+=("--allow-tool=$2"); shift 2 ;;
        *) echo "unsupported demo option: $1" >&2; exit 2 ;;
    esac
done

# Resolve aliases before checking overlap. Existing scratch roots are refused;
# nothing is deleted or overwritten, even when the caller supplies a bad path.
scratch="$("$python" - "$here" "$requested_scratch" <<'PY'
import sys
import tempfile
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
requested = sys.argv[2]
if not requested:
    print(tempfile.mkdtemp(prefix="skill-dispatch-demo.", dir="/tmp"))
else:
    candidate = Path(requested).resolve()
    if candidate.is_relative_to(repo) or repo.is_relative_to(candidate):
        sys.exit("scratch directory must not overlap the repository")
    if Path(requested).is_symlink():
        sys.exit("scratch directory must be a new directory, not a symlink")
    try:
        candidate.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        sys.exit("scratch directory already exists; choose a new directory")
    print(candidate)
PY
)"
for src in "$here"/workspaces/*/; do
    name="$(basename "$src")"
    dest="$scratch/$name"
    mkdir -- "$dest"
    cp -r -- "$src". "$dest/"
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
    args=(dispatch --root "$scratch" --task "$task" --backend "$backend" --agent "$agent")
    [ -n "$model" ] && args+=("--model=$model")
    args+=("${dispatch_options[@]}")
    "$python" "$here/orchestrator/skill_dispatch.py" "${args[@]}"
done

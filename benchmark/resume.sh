#!/usr/bin/env bash
# Resume a bench.sh run in place (e.g. after status `blocked` or `deadline`).
#
#   benchmark/resume.sh <out_dir>
#
# <out_dir> is benchmark/runs/<label>-<utc> from bench.sh/batch.sh. Reruns the
# same run-pipeline command on the same project root and run dir, so completed
# stages are reused, and writes result/stderr/time/summary into
# <out_dir>/resume-<utc>/. Assumes the live planner (batch.sh runs); the key is
# loaded as in bench.sh and never printed or written.
set -euo pipefail

out="$(cd "${1:?usage: resume.sh <out_dir>}" && pwd)"
proj="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$out/summary.json" ]; then
  run_dir="$("$proj/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_dir"])' "$out/summary.json")"
else
  # A killed bench.sh never wrote summary.json: take the only run dir of its root.
  runs=("$proj/tmp/bench/$(basename "$out")"/data/runs/*/)
  [ "${#runs[@]}" -eq 1 ] && [ -d "${runs[0]}" ] || { echo "cannot find one run dir for $out" >&2; exit 2; }
  run_dir="${runs[0]%/}"
fi
root="$(cd "$run_dir/../../.." && pwd)"
label="$(basename "$out" | sed -E 's/-[0-9]{8}T[0-9]{6}Z$//')"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
rout="$out/resume-$stamp"
mkdir -p "$rout"

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$HOME/.hermes/.env" ]; then
  OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$HOME/.hermes/.env" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
  export OPENROUTER_API_KEY
fi
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not available}"
export SCENERY_ANALYZE_WORKERS="${SCENERY_ANALYZE_WORKERS:-2}"
export TMPDIR="$root/tmp"
export SCENERY_ANALYZE_PROFILE="$rout/analyze_profile.json"
git -C "$proj" rev-parse HEAD > "$rout/git_head.txt"

set +e
/usr/bin/time -v -o "$rout/time.txt" \
  "$proj/.venv/bin/scenery-brief-clips" run-pipeline \
    --root "$root" --run-dir "$run_dir" \
    --brief "$root/brief.json" --live-planner --config "$root/config.yaml" \
    --theme "bench-$label" \
    --vision-agree --live-vision --allow-export \
    > "$rout/result.json" 2> "$rout/stderr.txt"
rc=$?
set -e
echo "$rc" > "$rout/exit_code.txt"
"$proj/.venv/bin/python" "$proj/benchmark/summarize.py" "$root" "$rout"
echo "$rout"
exit "$rc"

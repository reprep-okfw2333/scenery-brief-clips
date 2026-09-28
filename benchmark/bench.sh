#!/usr/bin/env bash
# Cold, fixed benchmark of run-pipeline.
#
#   benchmark/bench.sh <label> [seed_run_dir]
#
# With seed_run_dir, discover..apply_scores are copied from that run (same
# candidates, tiles and tile labels) and reused by the runner, so analyze,
# review and export see identical inputs. Caches are still cold.
#
# Every run gets a fresh project root under tmp/bench/<label>-<utc>/ so that no
# metadata, storyboard, analysis, or export cache is warm. Inputs are frozen:
# benchmark/improvement/{brief,plan,config} (2 ocean-wave clips, 2 ranked,
# 4 tiles, 2 analyzed, 60 s analysis cap). Search results themselves are live
# and can drift; the summary records candidate IDs so drift is visible.
#
# Vision: the wire in vision.yaml (approved for these benchmarks). The key is
# read from OPENROUTER_API_KEY, or from ~/.hermes/.env if unset. It is never
# printed or written.
set -euo pipefail

label="${1:?usage: bench.sh <label> [seed_run_dir]}"
seed="${2:-}"
proj="$(cd "$(dirname "$0")/.." && pwd)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
root="$proj/tmp/bench/$label-$stamp"
out="$proj/benchmark/runs/$label-$stamp"
mkdir -p "$root/tmp" "$out"
cp "$proj/vision.yaml" "$proj/planner.yaml" "$root/"
# Inputs must live inside the project root (config.py refuses outside paths).
# BENCH_INPUTS picks the frozen input set (default: the ocean-wave pair).
inputs="$proj/benchmark/${BENCH_INPUTS:-improvement}"
cp "$inputs/brief.json" "$inputs/plan.json" "$root/"
cp "$inputs/config.yaml" "$root/config.yaml"
echo "BENCH_INPUTS=${BENCH_INPUTS:-improvement}" > "$out/inputs.txt"

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$HOME/.hermes/.env" ]; then
  OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$HOME/.hermes/.env" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
  export OPENROUTER_API_KEY
fi
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not available}"

export SCENERY_ANALYZE_WORKERS="${SCENERY_ANALYZE_WORKERS:-2}"
export TMPDIR="$root/tmp"
export SCENERY_ANALYZE_PROFILE="$out/analyze_profile.json"

git -C "$proj" rev-parse HEAD > "$out/git_head.txt"
git -C "$proj" diff --stat > "$out/git_diff_stat.txt"
echo "SCENERY_ANALYZE_WORKERS=$SCENERY_ANALYZE_WORKERS" > "$out/env.txt"

run_dir_args=()
if [ -n "$seed" ]; then
  run_dir="$root/data/runs/seeded"
  mkdir -p "$run_dir"
  for f in candidates.json constraint.json discovery.json ranked.json ranked_before_vision.json \
           rejected.json vision_scores.json log.txt; do
    [ -e "$seed/$f" ] && cp "$seed/$f" "$run_dir/"
  done
  [ -d "$seed/vision_labels" ] && cp -r "$seed/vision_labels" "$run_dir/"
  # Export resolves formats from cached metadata; discovery is reused, so seed it.
  mkdir -p "$root/data/cache"
  cp -r "$seed/../../cache/metadata" "$root/data/cache/metadata"
  "$proj/.venv/bin/python" - "$seed/runner_state.json" "$run_dir/runner_state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
keep = ["discover", "rank", "agree_vision", "label_tiles", "apply_scores"]
state["completed"] = {k: v for k, v in state["completed"].items() if k in keep}
state["timing"] = {"stages": [], "retries": 0, "waiting_for_input_s": 0.0}
state.pop("active_execution_s", None)
json.dump(state, open(sys.argv[2], "w"), indent=2)
PY
  echo "$seed" > "$out/seed.txt"
  run_dir_args=(--run-dir "$run_dir")
fi

set +e
/usr/bin/time -v -o "$out/time.txt" \
  "$proj/.venv/bin/scenery-brief-clips" run-pipeline \
    --root "$root" "${run_dir_args[@]}" \
    --brief "$root/brief.json" --plan "$root/plan.json" --config "$root/config.yaml" \
    --theme "bench-$label" \
    --vision-agree --live-vision --allow-export \
    > "$out/result.json" 2> "$out/stderr.txt"
rc=$?
set -e
echo "$rc" > "$out/exit_code.txt"

"$proj/.venv/bin/python" "$proj/benchmark/summarize.py" "$root" "$out"
echo "$out"
exit "$rc"

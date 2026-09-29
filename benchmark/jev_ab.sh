#!/usr/bin/env bash
# Paired live A/B of Jev (docs/JEV.md) on batch1 briefs.
#
#   benchmark/jev_ab.sh ID [ID ...]        e.g. benchmark/jev_ab.sh R09 R07 R03 R05
#
# For each ID: arm J runs bench.sh with the live planner and BENCH_JEV=both;
# arm C then runs with J's query plan frozen (same searches), BENCH_JEV=off,
# and J's metadata cache pre-filled (fewer YouTube requests; C's discover time
# is therefore not cold). Everything else is cold in both arms.
# JEV_AB_GAP_S (default 300) seconds pass between runs to pace YouTube traffic.
# Stops at once if a run ends with status "blocked" (YouTube refused the host).
# Writes benchmark/runs/jev_ab-index-<utc>.tsv: <ID>\t<arm>\t<exit>\t<status>\t<out_dir>.
set -uo pipefail

proj="$(cd "$(dirname "$0")/.." && pwd)"
[ "$#" -gt 0 ] || { echo "usage: jev_ab.sh ID [ID ...]" >&2; exit 2; }
gap="${JEV_AB_GAP_S:-300}"
mkdir -p "$proj/benchmark/runs" "$proj/tmp"
index="$proj/benchmark/runs/jev_ab-index-$(date -u +%Y%m%dT%H%M%SZ).tsv"
: > "$index"
stdout_tmp="$(mktemp -p "$proj/tmp" jev-ab-stdout.XXXXXX)"
trap 'rm -f "$stdout_tmp"' EXIT

status_of() {  # run status from an out dir's result.json
  "$proj/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status"))' "$1/result.json" 2>/dev/null || echo missing
}

first=1
for id in "$@"; do
  inputs="$proj/benchmark/batch1/$id"
  [ -f "$inputs/brief.json" ] || { echo "skip $id: no brief" >&2; continue; }

  [ "$first" = 1 ] || sleep "$gap"
  first=0
  echo "=== $id J $(date -u +%H:%M:%SZ) ===" >&2; uptime >&2
  BENCH_INPUTS="batch1/$id" BENCH_LIVE_PLANNER=1 BENCH_JEV=both \
    "$proj/benchmark/bench.sh" "jevab-$id-J" > "$stdout_tmp"
  rc=$?; out_j="$(tail -n 1 "$stdout_tmp")"; [ -d "$out_j" ] || out_j=missing
  st="$(status_of "$out_j")"
  printf '%s\tJ\t%s\t%s\t%s\n' "$id" "$rc" "$st" "$out_j" >> "$index"
  echo "$id J exit=$rc status=$st out=$out_j" >&2
  [ "$st" = blocked ] && { echo "YouTube blocked the host; stopping" >&2; break; }

  root_j="$proj/tmp/bench/$(basename "$out_j")"
  run_j="$(ls -d "$root_j"/data/runs/* 2>/dev/null | head -1)"
  if [ -z "$run_j" ] || [ ! -f "$run_j/discovery.json" ]; then
    echo "$id: J arm has no discovery.json; skipping C" >&2
    continue
  fi
  cinputs="$proj/benchmark/jev_ab/inputs/$id"
  mkdir -p "$cinputs"
  cp "$inputs/brief.json" "$inputs/config.yaml" "$cinputs/"
  "$proj/.venv/bin/python" -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["query_plan"], open(sys.argv[2], "w"), indent=2)' \
    "$run_j/discovery.json" "$cinputs/plan.json"

  sleep "$gap"
  echo "=== $id C $(date -u +%H:%M:%SZ) ===" >&2; uptime >&2
  BENCH_INPUTS="jev_ab/inputs/$id" BENCH_JEV=off BENCH_SEED_METADATA="$root_j/data/cache/metadata" \
    "$proj/benchmark/bench.sh" "jevab-$id-C" > "$stdout_tmp"
  rc=$?; out_c="$(tail -n 1 "$stdout_tmp")"; [ -d "$out_c" ] || out_c=missing
  st="$(status_of "$out_c")"
  printf '%s\tC\t%s\t%s\t%s\n' "$id" "$rc" "$st" "$out_c" >> "$index"
  echo "$id C exit=$rc status=$st out=$out_c" >&2
  [ "$st" = blocked ] && { echo "YouTube blocked the host; stopping" >&2; break; }
done

echo "$index"

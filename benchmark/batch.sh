#!/usr/bin/env bash
# Sequential batch of bench.sh runs with the live planner.
#
#   benchmark/batch.sh <batch_name> [ID ...]
#
# batch_name is a directory under benchmark/ (e.g. batch1) whose subdirectories
# hold brief.json + config.yaml. With no IDs, every subdirectory with a
# brief.json runs, in sorted order. A failed run does not stop the batch.
# Writes benchmark/runs/<batch_name>-index-<utc>.tsv with lines
# <ID>\t<exit_code>\t<out_dir> and prints its path last.
set -uo pipefail

batch="${1:?usage: batch.sh <batch_name> [ID ...]}"
shift
proj="$(cd "$(dirname "$0")/.." && pwd)"
bdir="$proj/benchmark/$batch"
[ -d "$bdir" ] || { echo "no such batch dir: $bdir" >&2; exit 2; }

ids=("$@")
if [ "${#ids[@]}" -eq 0 ]; then
  for d in "$bdir"/*/; do
    [ -f "${d}brief.json" ] && ids+=("$(basename "$d")")
  done
  mapfile -t ids < <(printf '%s\n' "${ids[@]}" | sort)
fi
[ "${#ids[@]}" -gt 0 ] || { echo "no runs in $bdir" >&2; exit 2; }

mkdir -p "$proj/benchmark/runs"
index="$proj/benchmark/runs/$batch-index-$(date -u +%Y%m%dT%H%M%SZ).tsv"
: > "$index"
mkdir -p "$proj/tmp"
stdout_tmp="$(mktemp -p "$proj/tmp" batch-stdout.XXXXXX)"
trap 'rm -f "$stdout_tmp"' EXIT

for id in "${ids[@]}"; do
  if [ ! -f "$bdir/$id/brief.json" ]; then
    echo "skip $id: no brief.json" >&2
    printf '%s\t%s\t%s\n' "$id" "missing-input" "missing" >> "$index"
    continue
  fi
  echo "=== $batch/$id $(date -u +%H:%M:%SZ) ===" >&2
  uptime >&2
  # bench.sh prints the summary JSON, then the out dir as its last line.
  BENCH_INPUTS="$batch/$id" BENCH_LIVE_PLANNER=1 "$proj/benchmark/bench.sh" "$batch-$id" > "$stdout_tmp"
  rc=$?
  last="$(tail -n 1 "$stdout_tmp")"
  if [ -d "$last" ]; then out="$last"; else out="missing"; fi
  printf '%s\t%s\t%s\n' "$id" "$rc" "$out" >> "$index"
  echo "$id exit=$rc out=$out" >&2
done

echo "$index"

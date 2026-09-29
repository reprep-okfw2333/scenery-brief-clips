# step5-iceland: live 1080p + place-name brief (2026-09-29)

Brief: benchmark/step5-iceland/brief.json ("5 clips of waterfalls in Iceland,
1080p", written by an operator agent from docs/SEARCH_BRIEF.md). Live planner
and live vision (z-ai/glm-5.3-flash), cold caches, blend continuity, export on.

Two invocations on the step 5 working tree (the diff in git_diff_stat.txt):

1. `bench.sh` run, stopped by the owner's wrap-up in shortlist_review
   (exit 143). Its files are kept as `interrupted-*`.
2. Resumed with the same run-pipeline command plus
   `--acknowledge-uncertain shortlist_review`, after removing the partial
   `review/` frames (derived output). `result.json`, `stderr.txt`,
   `time.txt` and `summary.json` are from this resume. The resume's wall
   clock (13:35) covers only the stages after verify_review.

summary.json's `stages_s` keys stages by name, so the resumed "reused"
entries show 0 for the first-invocation stages. Executed times (s):

| discover | rank | label_tiles | analyze | verify_review | shortlist_review | label_strips | export | verify_export | sum |
|---|---|---|---|---|---|---|---|---|---|
| 196.8 | 8.4 | 89.5 | 266.5 | 82.7 | 125.1 | 95.4 | 461.8 | 131.9 | 1458 |

Yield: 17 candidates, 5 analyzed sources, 17 ranges, 16 excerpts, 0
continuity rejects; shortlist 5/5 (excluded: 4 visual rejects, 3 duplicates
of one shot across sources, 4 beyond n_clips); 5 clips delivered, all
1920x1080 H.264 (4.8-6.0 s); verify_export ok, request_fulfilled true.
Model calls: 1 planner, 26 tile, 16 strip.

Checked:
- 1080p cap from the brief reached export (plan/manifest max_height 1080).
  Every clip was a fresh 1080p acquisition: analysis copies are <=720p, so
  nothing was adoptable. Export cost ~92 s per clip vs ~16 s per adopted
  720p clip in the red deer run.
- Geography hint: strip labels geo supported 15, uncertain 1, conflicting 0.
  All planner queries named Iceland, so no off-place footage arrived; the
  exclusion path (geo conflicting) was not exercised live.
- Contact sheets of all 5 clips (1 fps): continuous shots of Icelandic
  waterfalls (Bjarnarfoss-like, Seljalandsfoss, Kirkjufellsfoss, Godafoss,
  the Dettifoss canyon). Quality gaps (plan step 7): the Dettifoss clip
  carries a "Beautiful World 4K" watermark and shows the fall only as
  distant mist; vision noted a small watermark on the Seljalandsfoss clip.

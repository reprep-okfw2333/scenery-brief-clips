# batch2: unattended-first track, live check (prepared 2026-09-29)

Same briefs as batch1 R05, R07, R09, same config EXCEPT max_rank_videos /
max_analyze_videos are left unset, so run-pipeline derives them from n_clips
(R05 10 clips -> 8 analyzed / 12 ranked; R07 4 -> 4 / 10; R09 6 -> 5 / 10).
Code: strip label budget, source scaling, soft place/setting vision rule,
auto-recovery, plus B2 (docs/PLAN-PROGRESS "Unattended-first track").

What each run checks, against batch1 (benchmark/RESULTS-2026-09-29-batch1.md):
- R05 "10 clips of Tokyo street traffic at night": source scaling (B1 8/10).
- R09 "6 European alpine lake clips": place rule (B1 3/6, place-driven).
- R07 "four clips of red deer in a misty forest": setting rule, label budget.

    benchmark/batch.sh batch2 [R09 R05 R07]

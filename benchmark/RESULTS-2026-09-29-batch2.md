# batch2: unattended-first track, live (2026-09-29 evening)

Code: working tree after 349579d with the strip label budget, source scaling
from n_clips, the soft place/setting vision rule (vision_label_v2) and
automatic recovery (docs/PLAN-PROGRESS "Unattended-first track"). Inputs:
benchmark/batch2/ = batch1 R05, R07, R09 briefs, same config except
max_rank_videos / max_analyze_videos unset (derived from n_clips). Live
GLM 5.3 Flash, cold roots, live planner.

## How the runs went

- R09 (16:41) and R05 (16:45) ended `blocked`: YouTube's bot check refused
  the host mid-run (R09 after one analyzed source, R05 already in discovery
  metadata). The circuit breaker stopped all further contact, as designed.
  R07 started 16:48 and ran clean, so the refusals were brief.
- R09 was resumed in place (benchmark/resume.sh, the `blocked` instruction:
  rerun the same command) at 17:01 and completed.
- R05's discovery had been degraded by the refusals (9 of 12 candidates), so
  it was rerun fresh at 17:08. That run was killed in discover by a session
  restart; resumed at 19:06 with resume.sh, the runner redid discover
  automatically (`auto_recoveries: {"discover": 1}`, first live use) and
  completed.

## Numbers (vs batch1, same briefs)

| id | request | clips B1 | clips B2 | verify | wall B1 | wall B2 | calls B1 | calls B2 |
|---|---|---|---|---|---|---|---|---|
| R05 | 10 clips of Tokyo street traffic at night | 8/10 | **10/10** | ok | 26:02 | 29:58 | 39 | 67 |
| R07 | four clips of red deer in a misty forest | 4/4 | 4/4 | ok | 13:07 | **10:34** | 38 | 56 |
| R09 | 6 European alpine lake clips | 3/6 | 3/6 | ok | 17:03 | 4:12 + 6:24 (blocked, resumed) | 40 | 51 + 55 |

Stage times (s), B1 -> B2:

| stage | R05 | R07 |
|---|---|---|
| discover | 229 -> 108 | 187 -> 103 |
| label_tiles | 212 -> 145 | 36 -> 91 |
| analyze | 343 -> 592 (5 -> 8 sources) | 191 -> 219 |
| label_strips | 101 -> 57 | 161 -> 10 |
| export | 402 -> 572 (8 -> 10 clips) | 68 -> 57 |
| verify_export | 105 -> 63 | 44 -> 10 |

- Strip label budget: R07 labeled 4 moments (17 recorded "not labeled:
  enough clips kept"); R09 labeled 4 (6 skipped as duplicates of a keep); R05
  stopped at 10 keeps (7 not labeled). Strip labeling fell 161 -> 10 s (R07).
- Source scaling: R05 analyzed 8 sources (B1 pinned 5) and filled 10/10.
- Model calls rose because rank now defaults to 10 videos when config is
  silent (B1 pinned 5), so more tiles are labeled (R07 tile labels 36 -> 91 s).
  Follow-up: tie rank to the analyzed count.

## Clips by eye (17 delivered)

Judge: Opus, contact sheets (benchmark/batch2/judge/labels.json, mapping.tsv;
sheets not committed). B1's eye labels were Sonnet's (checked in part), so
the comparison is indicative.

| | good | acceptable | bad | usable |
|---|---|---|---|---|
| B1 (R05+R07+R09) | 5 | 6 | 4 | 11/15 |
| B2 | 10 | 6 | 1 | 16/17 |

No cut inside any clip (c11, a Shibuya pan, checked at 2 fps). The bad one
(R05 c15) is a very dark rainy highway. Acceptable ones carry a watermark
(R07), little traffic or darkness (R05), or the soft rules at work.

## Findings

- R09 did not improve (3/6). The place rule worked (every "not recognizably
  European" lake was kept; the only reject was a title overlay at an
  American lodge), but analysis yielded only 10 moments from 5 sources and 6
  were near-identical repeats of a kept moment (long static lake streams).
  Source scaling gives 5 sources for 6 clips, the same as B1's pin. Next:
  top up with more sources when the shortlist is short.
- Soft setting lets a subject-true clip through without the setting (R07
  c01: a stag in bracken, no mist or forest; B1 rejected that moment). This
  follows the owner's direction; flagged for the owner.
- Brief YouTube refusals turn a run into `blocked` although the host is
  accepted again minutes later; a rerun fixes it. Possible follow-up: one
  wait-and-retry before the breaker trips.

# Jev live A/B (2026-09-29): one paired brief

Code: working tree after 87f17c3 with jev.py (docs/JEV.md), full suite 958 passed and 1
xfailed. Harness: `benchmark/jev_ab.sh` (arm J: live planner with `jev_rank` and
`jev_note_check` on; arm C: J's query plan frozen, Jev off, J's metadata cache pre-filled).
The batch config is the same as batch1 (5 ranked sources x 6 tiles, 5 analyzed, 90 s), with
cold caches otherwise. The host was quiet. Index: benchmark/runs/jev_ab-index-20260929T144720Z.tsv.
Summary: `benchmark/jev_ab_summary.py <index>`.

Planned: R09, R07, R10, R03 (8 runs, about 3 h). The owner stopped it after the R09 pair to
free the host. Run the rest later with `benchmark/jev_ab.sh R07 R10 R03`.

## R09 "6 European alpine lake clips, 5-10 seconds, 720p"

| | J (Jev) | C (no Jev, same plan) | batch1 B1 (search order, 87f17c3) |
|---|---|---|---|
| status | completed, unattended | completed, unattended | completed |
| clips delivered / asked | 4 / 6 | 3 / 6 | 3 / 6 |
| blind eye verdicts | 2 good, 2 bad | 2 good, 1 acceptable | 2 good, 1 acceptable |
| usable clips | **2** | **3** | 3 |
| wall | 14:11 | 11:16 | 17:03 |
| stage sum | 849 s | 675 s | 1021 s |
| model calls (planner + vision) | 44 | 38 | 40 |
| metadata lookups | 6 | 5 (cache pre-filled) | 20 |
| excerpts | 21 | 14 | 15 |
| Jev calls / cost | 25 rank + 12 notes / $0.0016 | 0 | 0 |

Ranked sources:

- J: THE ALPS 8K (0 clips), Alpine Elegance ArtHouse (1 good: Austrian-looking lake with a
  ferry), VINDORA Matterhorn (1 good), European Mountain Lake Ambience "Living Backdrop"
  (1 **bad**: a watercolor-style illustration, not footage), 4K Crystal Alpine Lake (1 **bad**:
  Rockies-looking lake, not Europe).
- C: Hiking Alpine Lake Sawtooth Idaho (0), Colorado Lake City Alpine 50 race (0), 4K Crystal
  Alpine Lake (0), VINDORA Matterhorn (1 good, byte-identical to J's), 5 Scenic Alpine Lake
  Walks In Switzerland (1 good, 1 acceptable).

Judging: sonnet-high, blind to the arm, 1 fps contact sheets plus a scene-score scan (no cuts
in any clip). The orchestrator re-checked both "bad" sheets and agrees. Record:
benchmark/jev_ab/judge/ (labels.json, mapping.json, sheets/).

## Reading

- On this one brief Jev did what the offline replay predicted for ranking: it moved the Idaho
  hike, the Colorado race, Banff, Montana and "Moraine Lake" out of the rank slots, and J
  analyzed 5 Alps-titled sources. That gave more excerpts (21 vs 14) and more clips (4 vs 3).
- It still delivered **fewer usable clips (2 vs 3)**, for two reasons, each visible in the
  records:
  1. Jev answered `place: different` for "4K Crystal Alpine Lake" on full metadata but
     kept it: the score (usable, subject, conditions) ignores the place answer, so it scored
     0.57 > 0.35. Offline check of a place rule (reject when the brief names a place and
     P(place=different) >= 0.5): it rejects 14 of 184 eval sources, 12 reference-no and 2
     unsure. The 2 "productive" ones are the Alaska aurora (Norway brief) and the Australian
     camels (Sahara brief), both truly off-place. **Recommended next change.**
  2. An illustration channel (12 views) passed everything: Jev from metadata ("relaxation
     film", place matches), tile and strip vision (keep; the note said "painterly panorama"),
     and the note check (0.09; its criteria do not mention paintings or renders). Adding "not
     real camera footage (painting, illustration, render)" to the note check criteria (question
     version bump) would target it. Not tested.
- Search drift inside the pair: the same frozen queries 20 minutes apart returned partly
  different hits. C saw the Switzerland walks video and the Colorado race video, and J did
  not. So even the paired design compares slightly different pools.
- Time follows yield here: J's extra stage time is strip labels (103 vs 49 s) and export (107
  vs 67 s) for more excerpts and clips. Jev's own time is a few seconds (37 calls at about
  0.25 s, 4 concurrent). C's discovery was not cold (pre-filled metadata).
- Earlier B2 speedups, first measured live (J vs B1, both cold): discover 206 -> 113 s (20 -> 6
  metadata lookups, early stop), label_strips 174 -> 103 s with more excerpts (4 concurrent
  calls), verify_export 68 -> 12 s (reuses verify_review's decodes).

One brief cannot say whether Jev helps overall. The offline evidence (benchmark/jev_eval/)
still favors ranking; this run shows that `place` must count and that non-footage is not
covered. Keep `jev_rank` and `jev_note_check` off by default until the place rule is in and
the remaining pairs have run.

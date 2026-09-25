# Reliability and autonomous execution pass

Scope: this checkout only. Baseline HEAD 4237452, clean and equal to origin/master; 374 tests passed in 179.73s. Doctor passes. Historical runs named elsewhere are not present in this checkout (data was empty).

User goal: deterministic stage progression with minimal agent orchestration; models only where subjective judgment is needed; reliability, less repeated model work, and measured end-to-end latency improvements. No relaxation of acceptance, continuity, geometry, provenance, or shortfall rules. Jev stays off. No changes to sibling checkouts.

## Acceptance criteria and order

1. Reproduce and fix confirmed runner resume/invalidation defects before adding live adapters. Offline tests must challenge changed inputs, tampered outputs, and interrupted work; no stale output may count as verified completion.
2. Add explicitly opted-in live adapter wiring to the existing runner, retaining offline/injected operation and exact vision/export consent gates. A frozen plan must make zero planner calls. Existing stage functions remain authoritative. A normal CLI invocation must be able to progress without Python caller injection or an agent manually walking commands.
3. Add narrowly scoped reuse of successful image judgments if review establishes safe binding: image content, exact prompt, wire identity and policy. Failed/malformed judgments cannot become cache hits. Unchanged successful items must not require repeat model calls after retry. Preserve deterministic ordering and existing decisions.
4. Run focused regressions and the full suite after each dependent slice. Independently review changes. Record actual live benchmark counts, stage times, model calls where measurable, and unknown token usage as unknown. Do not claim quality or speed from fakes.

## Live authorization

User explicitly approved a bounded live benchmark: two 720p ocean-wave clips; visual judgments through configured codex-login / gpt-6-sol; selected-section analysis downloads and final export enabled. GLM 5.3 Flash through OpenRouter, high reasoning, is authorized as a development helper only. No cookies, full-film downloads, or alternate vision provider. All run outputs, caches, tests and media temps stay here.

Bound initial search to 6 hits / 6 metadata fetches, rank at most 2 sources and 4 tiles each, analyze at most 2 sources with 60 seconds per source. Retain honest shortfalls. Network refusal must be recorded rather than bypassed or hidden. A repeated benchmark is not a controlled speed comparison unless input sets and cache conditions match.

## Implemented; verification in progress

- Reproduced changed discovery bytes and interrupted-stage promotion as unsafe reuse. Completed stages now record SHA-256 output checkpoints and state is saved after each stage. Missing/changed outputs require an explicit recovery acknowledgment; interrupted external stages are not promoted based on file existence. Recovery clears dependent completion records.
- Reproduced corrupt exported media being reported complete from a cached verifier report. Final export verification now executes on every resume; no repeat model calls or re-encodes are needed for unchanged outputs.
- Reproduced discovery ignoring stricter configured search/metadata caps and geometry. Runner now applies caps and geometry without loosening the brief.
- CLI `--live-vision` connects the existing configured wire for both subjective stages; `--vision-agree` and export agreement remain required. Without `--live-vision`, captured-judgment behavior remains unchanged. CLI integration tests exercise actual stages with only external systems faked.
- Run-local image checkpoints reuse successful normalized judgments, binding image bytes, full prompt, backend/model/endpoint and schema. Per-key file locks prevent duplicate concurrent calls for an identical image. Malformed checkpoints are misses, failed judgments are not cached, and current strip flags/generation are applied after lookup.
- Focused checks passed: 40 tests before adding the discovery-cap regression; 23 runner tests after it. The earlier full suite passed 376 tests before live-vision/checkpoint/cap additions. A later foreground full-suite invocation timed out without a usable result; no pytest process remained, and a tracked background rerun is in progress. These are not final full-suite counts.
- Approved live run: `data/runs/20260925T180736Z`, logs `benchmark/improvement/live-first.*`. Started before the checkpoint/cap additions; it is exercising live automatic progression, not a before/after speed comparison. It has reached review-frame extraction without agent stage handoffs.

## Findings under investigation

- Correction after reading CLI: live search, storyboard, analysis, and export ports already exist. The CLI lacks live vision callers; its direct Python runner defaults to empty ports.
- Resume integrity and image-label checkpoint seams are undergoing separate bounded read-only reviews.
- Documentation drift: SEARCH_BRIEF.md still says implemented discovery is unbuilt; RUNNER.md opens with pre-runner behavior; ARCHITECTURE.md names an older checkout path. Correct affected docs with verified implementation changes.

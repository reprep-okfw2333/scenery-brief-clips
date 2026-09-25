# Live validation goal (proposal)

This is a proposal for a later `/goal`. Nothing here is running yet, and it is not a request to go to YouTube now. It describes one live check of the clip runner so we can record what a real run actually costs in time and tokens, with all the safety gates kept in place.

## Pasteable /goal block

```
/goal Run one live check of the clip runner and record the real cost
outcome: In /root/github-checkouts/reprep-okfw2333/scenery-brief-clips, use the existing run-pipeline command on one frozen brief the user supplies. Show the vision model from vision-show and wait for a yes before any picture labeling. Do not call Jev. Do not lower resolution, aspect, or clip count. Do not pad a shortfall. Export only after an explicit allow, and only the moments the existing shortlist selected. 720p remains the cap.
verify: verify --require-export prints ok true for that run. The clip files named in the manifest exist. A short written record in docs/LIVE_VALIDATION_RESULT.md gives the run id, selected count, exported count, shortfall explanation, stage times from runner_state.json, model-call counts, and token usage where the provider reported it. Unknown token usage stays unknown, not zero. A person looks at the exported clips and the record says whether each one matches the brief or why it does not.
constraints: Cookies stay off unless the user opts in. No API keys in yaml or the result note. Do not treat a metadata hit as a verified clip. Do not change acceptance, continuity, or export rules to make the run succeed. If YouTube asks for a sign-in, stop and say so. Do not route traffic through a home IP or a paid proxy.
boundaries: Edit only this checkout. Do not touch /root/projects/scenery-clips, /root/projects/scenery-brief-clips, /root/experiments/scenery-brief-lab, or global Hermes skills.
stop when: Stop before any download or picture label if the user has not agreed to the named vision model and has not allowed export. If the host is blocked by YouTube's bot check, report that and do not weaken the gates. Completion of this live check is what can support a speed or token claim. The earlier offline runner tests cannot.
```

Note: the run above is deliberately one frozen brief and one pass, with no tuning mid-run. The record it leaves is meant to be honest about gaps — a shortfall is explained, never padded, and any token count the provider does not report is left as unknown. Treat that result note as the only evidence for cost or speed claims.

- This proposal is not started.
- The offline runner tests passed 16 of 16 in tests/test_runner_contract.py.
- A full suite was 373 passed before this 16th test was added.
- Do not start this live goal until the user pastes it.
- A bot check blocked a live search on this host on 2026-09-25.

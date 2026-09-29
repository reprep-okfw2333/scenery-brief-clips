# Brief-writing evaluation (plan step 5)

Can an operator agent turn a plain request into a valid, faithful
`search_brief_v1` from the contract doc alone? A Sonnet sub-agent (high
effort) played the operator with only docs/SEARCH_BRIEF.md (schema section)
and SKILL.md step 5, for the same 12 requests (listed in each notes.json).

- `before/`: old contract doc, 2026-09-29 before step 5. 10 briefs written;
  2/10 valid under the old validator; places, 1080p and non-default durations
  not expressible. Asked on R06 (no count) and R12 (vertical).
- `after/`: rewritten contract. 9 briefs written, 9/9 valid under the new
  validator; asked on R06, R08 (4K) and R12.
- `score.py DIR`: validates each brief with the old validator (from git
  history, save it first: `git show 57f29d3:src/scenery_brief_clips/brief.py >
  tmp/brief_eval/brief_old.py`) and the current one.

The agent cannot see the validator; "valid" is measured afterwards.

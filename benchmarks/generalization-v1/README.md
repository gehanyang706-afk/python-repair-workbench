# Generalization v1

Two additional historical boltons bugs, kept separate from the original four-task comparison set:

- Empty MultiReplace mapping: preserve input unchanged while retaining nonempty replacements.
- Unit-factor backoff: handle valid equal bounds and reject unreachable bounds with inferred count.

Full original modules and official fixes are pinned in manifest.json with SHA-256 hashes and full upstream Git commits. Source copyright notices and BSD licenses are preserved. Tests are locally authored and split into visible/holdout scopes; oracle and holdout files are never provided to the Agent.

This is additional-task coverage within one project, not cross-project generalization evidence. Only fixture qualification has run; no real model has answered these tasks. See docs/reliability-v2.md for the evaluation protocol and the corrected stop=0 fixture expectation.

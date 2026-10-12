# Real bugs v1

Four historical boltons bugs represented by complete, unmodified upstream modules at pinned commits. This is a module-level dataset, not a full-repository benchmark.

`manifest.json` records upstream parent/fix commits, source paths, task descriptions and SHA-256 hashes. For each task:

- `source/`: original module, empty package initializer, upstream BSD license.
- `visible/`: locally authored public regression tests.
- `holdout/`: locally authored independent acceptance tests, excluded from Agent inputs.
- `oracle/`: original module at the official fixed commit, excluded from Agent inputs.

Upstream source can be verified with `git show <base_commit>:<allowed_path>` and `git show <fix_commit>:<allowed_path>` in https://github.com/mahmoud/boltons. The manifest records full commit IDs. Vendored bytes preserve the copyright notices; each task carries the upstream LICENSE.

See `docs/evaluation.md` for qualification rules, execution, cost estimates and limitations. Qualification success measures fixture validity, not model capability. Do not tune prompts against holdout failures and then describe this set as unseen evaluation.

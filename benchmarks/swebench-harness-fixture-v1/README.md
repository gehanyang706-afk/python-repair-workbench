# Official-harness transport fixture

This is a two-test synthetic fixture, not a SWE-bench task. It exercises the
unmodified, pinned official run_instance/get_eval_report using a local image.
One test fails before the patch and passes after it; the other must stay passing.
Any success here is harness integration evidence, never a benchmark score.

The image inherits an already installed workbench test image. Building installs
Git; evaluation itself has no network and never pulls images. No model is used.

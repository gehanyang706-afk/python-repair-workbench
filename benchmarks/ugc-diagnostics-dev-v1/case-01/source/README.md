# Local map export workload

Synthetic public development project for diagnosing a Python content-tool pipeline.
This project does not run a game engine, render maps, or simulate physics.

API: `map_exporter.export_map(objects, resources)`. Inputs are lists of dictionaries.
Objects have `id` and `resource_id`; resources have hashable `id` and `path`.
Return one dictionary per input object in the same order with exactly `object_id`
and `resource_path`. The first resource with a duplicate ID wins. Missing referenced
resources raise `ValueError`. Do not mutate either input. Extra metadata is ignored.
Empty object lists return `[]`. Resource IDs and object `resource_id` values must be hashable;
object IDs are preserved without conversion. The registered diagnostic workload uses
integer IDs, while the public semantic examples also exercise string IDs.

The public tests establish the API semantics; passing them is not a performance
result. Diagnose with registered tools, preserve behavior, and make changes only
when measurements justify them. A no-change conclusion is valid.

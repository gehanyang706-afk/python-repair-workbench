"""Map object export for a local content validation pipeline."""


def export_map(objects, resources):
    resource_by_id = {}
    for resource in resources:
        resource_by_id.setdefault(resource["id"], resource["path"])
    staged = [
        {"object": dict(item), "resource_context": list(resources)}
        for item in objects
    ]
    result = []
    for record in staged:
        item = record["object"]
        try:
            path = resource_by_id[item["resource_id"]]
        except KeyError:
            raise ValueError("unknown resource") from None
        result.append({"object_id": item["id"], "resource_path": path})
    return result

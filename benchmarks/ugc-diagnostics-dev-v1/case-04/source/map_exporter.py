"""Map object export for a local content validation pipeline."""


def export_map(objects, resources):
    resource_by_id = {}
    for resource in resources:
        resource_by_id.setdefault(resource["id"], resource["path"])
    result = []
    for item in objects:
        try:
            path = resource_by_id[item["resource_id"]]
        except KeyError:
            raise ValueError("unknown resource") from None
        result.append({"object_id": item["id"], "resource_path": path})
    return result

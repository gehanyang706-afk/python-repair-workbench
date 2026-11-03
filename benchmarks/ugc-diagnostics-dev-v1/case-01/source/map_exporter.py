"""Map object export for a local content validation pipeline."""


def export_map(objects, resources):
    result = []
    for item in objects:
        matched = None
        for resource in resources:
            if resource["id"] == item["resource_id"]:
                matched = resource
                break
        if matched is None:
            raise ValueError("unknown resource")
        result.append({"object_id": item["id"], "resource_path": matched["path"]})
    return result

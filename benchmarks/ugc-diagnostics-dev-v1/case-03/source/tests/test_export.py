"""Public semantic checks; these are development examples, not hidden scoring."""

from copy import deepcopy
import unittest

from map_exporter import export_map


class ExportMapTests(unittest.TestCase):
    def test_preserves_object_order(self):
        objects = [{"id": "second", "resource_id": "b"}, {"id": "first", "resource_id": "a"}]
        resources = [{"id": "a", "path": "a.png"}, {"id": "b", "path": "b.png"}]
        self.assertEqual(export_map(objects, resources), [
            {"object_id": "second", "resource_path": "b.png"},
            {"object_id": "first", "resource_path": "a.png"},
        ])

    def test_first_duplicate_resource_wins(self):
        objects = [{"id": "o", "resource_id": "r"}]
        resources = [{"id": "r", "path": "first.png"}, {"id": "r", "path": "later.png"}]
        self.assertEqual(export_map(objects, resources), [
            {"object_id": "o", "resource_path": "first.png"},
        ])

    def test_missing_resource_raises_value_error(self):
        with self.assertRaises(ValueError):
            export_map([{"id": "o", "resource_id": "missing"}], [{"id": "r", "path": "r.png"}])

    def test_inputs_remain_unchanged(self):
        objects = [{"id": "o", "resource_id": "r", "metadata": {"tags": ["spawn"]}}]
        resources = [{"id": "r", "path": "r.png", "metadata": {"size": [8, 8]}}]
        original = deepcopy((objects, resources))
        export_map(objects, resources)
        self.assertEqual((objects, resources), original)

    def test_empty_objects_return_empty_list(self):
        self.assertEqual(export_map([], []), [])
        self.assertEqual(export_map([], [{"id": "r", "path": "r.png"}]), [])

    def test_duplicate_object_ids_are_not_dropped(self):
        objects = [{"id": "same", "resource_id": "r"}, {"id": "same", "resource_id": "r"}]
        self.assertEqual(export_map(objects, [{"id": "r", "path": "r.png"}]), [
            {"object_id": "same", "resource_path": "r.png"},
            {"object_id": "same", "resource_path": "r.png"},
        ])

    def test_exception_does_not_modify_inputs(self):
        objects = [{"id": "a", "resource_id": "r"}, {"id": "b", "resource_id": "missing"}]
        resources = [{"id": "r", "path": "r.png"}]
        original = deepcopy((objects, resources))
        with self.assertRaises(ValueError):
            export_map(objects, resources)
        self.assertEqual((objects, resources), original)


if __name__ == "__main__":
    unittest.main()

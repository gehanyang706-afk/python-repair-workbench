"""Task-list query-count and public-shape contracts, without a database service."""

from contextlib import contextmanager
import json
import unittest
from repair_workbench.mysql_store import MySQLStore


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.selected = rows

    def execute(self, query, args=None):
        if "WHERE id=%s" in query:
            self.selected = [r for r in self.rows if r["id"] == args[0]]
        elif query.startswith("SELECT id "):
            self.selected = [{"id": r["id"]} for r in self.rows]
        else:
            self.selected = self.rows

    def fetchall(self):
        return [dict(r) for r in self.selected]

    def fetchone(self):
        return dict(self.selected[0]) if self.selected else None


class StoreProbe(MySQLStore):
    def __init__(self, rows):
        super().__init__(None)
        self.rows = rows
        self.connections = 0

    @contextmanager
    def transaction(self):
        self.connections += 1
        yield Cursor(self.rows)


class MySQLListTests(unittest.TestCase):
    def make(self):
        return StoreProbe(
            [
                {
                    "id": str(i),
                    "idempotency_key": "request-key",
                    "request_digest": "digest",
                    "request": json.dumps({"task": "fixture"}),
                    "result": None,
                    "state": "completed",
                }
                for i in range(3)
            ]
        )

    def test_history_list_uses_one_transaction_not_one_per_task(self):
        store = self.make()
        rows = store.list()
        self.assertEqual(len(rows), 3)
        self.assertEqual(store.connections, 1)

    def test_list_preserves_detail_shape_and_redacts_request_metadata(self):
        store = self.make()
        rows = store.list()
        self.assertEqual(rows, [store.get(str(i)) for i in range(3)])
        for row in rows:
            self.assertNotIn("idempotency_key", row)
            self.assertNotIn("request_digest", row)
            self.assertIsInstance(row["request"], dict)

    def test_empty_list_still_has_one_query_boundary(self):
        store = StoreProbe([])
        self.assertEqual(store.list(), [])
        self.assertEqual(store.connections, 1)

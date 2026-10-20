"""MySQL authority for leased tasks, budget reservations and immutable receipts."""

from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from .session import BudgetExceeded


class OwnershipLost(BudgetExceeded):
    pass


class IdempotencyConflict(ValueError):
    pass


def canonical_digest(request):
    return hashlib.sha256(
        json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = "repair_workbench"

    @classmethod
    def load(cls, path=None):
        from dotenv import dotenv_values

        values = dotenv_values(path) if path else {}

        def get(name, default=None):
            return os.environ.get(name, values.get(name, default))

        database = get("REPAIR_DB_NAME", "repair_workbench")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
            raise ValueError("invalid database name")
        user, password = get("REPAIR_DB_USER"), get("REPAIR_DB_PASSWORD")
        if not user or not password:
            raise ValueError("REPAIR_DB_USER and REPAIR_DB_PASSWORD are required")
        return cls(
            get("REPAIR_DB_HOST", "127.0.0.1"),
            int(get("REPAIR_DB_PORT", 33079)),
            user,
            password,
            database,
        )

    def connect(self):
        import pymysql

        connection = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
        return connection


@dataclass(frozen=True)
class Lease:
    task_id: str
    owner: str
    generation: int


class MySQLStore:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def transaction(self):
        db = self.settings.connect()
        try:
            with db.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                db.begin()
                yield cursor
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate(self):
        # Additive, versioned DDL only. Existing SQLite files are never opened.
        source = Path(__file__).parent / "migrations/001_mysql.sql"
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT GET_LOCK('repair_workbench_schema_v1', 10) AS acquired"
            )
            if cursor.fetchone()["acquired"] != 1:
                raise RuntimeError("schema migration lock unavailable")
            try:
                for statement in source.read_text().split(";"):
                    if statement.strip():
                        cursor.execute(statement)
            finally:
                cursor.execute("SELECT RELEASE_LOCK('repair_workbench_schema_v1')")

    @staticmethod
    def decode(row):
        if row is None:
            return None
        row = dict(row)
        for key in ("request", "result", "payload", "metadata", "usage_json"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
        for key, value in row.items():
            if isinstance(value, datetime):
                row[key] = value.isoformat() + "Z"
        return row

    @staticmethod
    def emit(cursor, task_id, generation, payload):
        cursor.execute(
            "INSERT INTO task_events(task_id,generation,payload) VALUES(%s,%s,%s)",
            (task_id, generation, encoded(payload)),
        )

    def guard(self, cursor, lease, *, allow_cancel=False):
        cursor.execute(
            "SELECT *, lease_until > UTC_TIMESTAMP(6) AS alive, agent_deadline<=UTC_TIMESTAMP(6) AS budget_expired FROM repair_tasks WHERE id=%s FOR UPDATE",
            (lease.task_id,),
        )
        row = cursor.fetchone()
        valid_states = {"running", "cancelling"} if allow_cancel else {"running"}
        if (
            not row
            or row["lease_owner"] != lease.owner
            or row["generation"] != lease.generation
            or not row["alive"]
            or row["state"] not in valid_states
        ):
            raise OwnershipLost("lease_expired_or_replaced")
        return self.decode(row)

    def create(self, request, key, digest=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ValueError("Idempotency-Key is required (1-128 ASCII key characters)")
        digest = digest or canonical_digest(request)
        task_id = uuid4().hex
        import pymysql

        try:
            with self.transaction() as c:
                c.execute(
                    "INSERT INTO repair_tasks(id,idempotency_key,request_digest,request) VALUES(%s,%s,%s,%s)",
                    (task_id, key, digest, encoded(request)),
                )
                self.emit(c, task_id, 0, {"event": "queued"})
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] != 1062:
                raise
            with self.transaction() as c:
                c.execute("SELECT * FROM repair_tasks WHERE idempotency_key=%s", (key,))
                row = self.decode(c.fetchone())
            if row is None or row["request_digest"] != digest:
                raise IdempotencyConflict("idempotency_key_content_conflict") from None
            return self.get(row["id"])
        return self.get(task_id)

    def existing_request(self, key, digest):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ValueError("Idempotency-Key is required (1-128 ASCII key characters)")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT id,request_digest FROM repair_tasks WHERE idempotency_key=%s",
                (key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise IdempotencyConflict("idempotency_key_content_conflict")
        return self.get(row["id"])

    def get(self, task_id):
        with self.transaction() as c:
            c.execute("SELECT * FROM repair_tasks WHERE id=%s", (task_id,))
            row = self.decode(c.fetchone())
        if row:
            row.pop("idempotency_key", None)
            row.pop("request_digest", None)
        return row

    def list(self):
        with self.transaction() as c:
            c.execute("SELECT id FROM repair_tasks ORDER BY created_at DESC LIMIT 100")
            ids = [r["id"] for r in c.fetchall()]
        return [self.get(identity) for identity in ids]

    def claim(self, owner, lease_seconds, task_id=None):
        if not re.fullmatch(r"[0-9a-f]{32}", owner) or not 0.2 <= lease_seconds <= 300:
            raise ValueError("invalid worker identity or lease duration")
        with self.transaction() as c:
            predicate = "(state='queued' OR (state IN ('running','cancelling') AND lease_until<=UTC_TIMESTAMP(6)))"
            if task_id is not None:
                predicate += " AND id=%s"
            c.execute(
                "SELECT * FROM repair_tasks WHERE "
                + predicate
                + " ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (task_id,) if task_id is not None else (),
            )
            row = c.fetchone()
            if row is None:
                return None
            generation = row["generation"] + 1
            c.execute(
                "UPDATE task_attempts SET state='expired',finished_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND state='running'",
                (row["id"],),
            )
            c.execute(
                "UPDATE model_calls SET state=CASE WHEN state='started' THEN 'unknown' ELSE 'abandoned' END, updated_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND state IN ('started','reserved')",
                (row["id"],),
            )
            c.execute(
                "UPDATE repair_tasks SET state=IF(state='cancelling','cancelling','running'),lease_owner=%s,generation=%s,lease_until=TIMESTAMPADD(MICROSECOND,%s,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (owner, generation, int(lease_seconds * 1e6), row["id"]),
            )
            c.execute(
                "INSERT INTO task_attempts(task_id,generation,owner,state) VALUES(%s,%s,%s,'running')",
                (row["id"], generation, owner),
            )
            self.emit(
                c,
                row["id"],
                generation,
                {
                    "event": "claimed",
                    "generation": generation,
                    "previous_generation": row["generation"],
                },
            )
        return Lease(row["id"], owner, generation)

    def heartbeat(self, lease, seconds):
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            c.execute(
                "UPDATE repair_tasks SET lease_until=TIMESTAMPADD(MICROSECOND,%s,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (int(seconds * 1e6), lease.task_id),
            )
            c.execute(
                "UPDATE task_attempts SET heartbeat_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND generation=%s",
                (lease.task_id, lease.generation),
            )

    def check(self, lease):
        with self.transaction() as c:
            row = self.guard(c, lease, allow_cancel=True)
            c.execute(
                "SELECT agent_deadline<=UTC_TIMESTAMP(6) AS expired FROM repair_tasks WHERE id=%s",
                (lease.task_id,),
            )
            row["deadline_expired"] = bool(c.fetchone()["expired"])
        return row

    def begin_agent(self, lease):
        with self.transaction() as c:
            row = self.guard(c, lease)
            c.execute(
                "UPDATE repair_tasks SET agent_deadline=COALESCE(agent_deadline,TIMESTAMPADD(MICROSECOND,%s,UTC_TIMESTAMP(6))) WHERE id=%s",
                (int(row["request"]["deadline"] * 1e6), lease.task_id),
            )

    def event(self, lease, payload, phase=None):
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            self.emit(c, lease.task_id, lease.generation, payload)
            if phase:
                c.execute(
                    "UPDATE repair_tasks SET phase=%s WHERE id=%s",
                    (phase, lease.task_id),
                )

    def cancel(self, task_id):
        with self.transaction() as c:
            c.execute(
                "SELECT state,generation FROM repair_tasks WHERE id=%s FOR UPDATE",
                (task_id,),
            )
            row = c.fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] in {"queued", "running"}:
                state = "cancelled" if row["state"] == "queued" else "cancelling"
                c.execute(
                    "UPDATE repair_tasks SET state=%s,updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                    (state, task_id),
                )
                self.emit(c, task_id, row["generation"], {"event": state})

    def finish(self, lease, result, *, state="completed", reason=None):
        if state not in {"completed", "interrupted", "error", "cancelled"}:
            raise ValueError("invalid terminal state")
        with self.transaction() as c:
            row = self.guard(c, lease, allow_cancel=True)
            state = "cancelled" if row["state"] == "cancelling" else state
            c.execute(
                "UPDATE repair_tasks SET state=%s,phase='finished',result=%s,error_type=%s,lease_until=NULL,updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (state, encoded(result), reason, lease.task_id),
            )
            c.execute(
                "UPDATE task_attempts SET state=%s,finished_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND generation=%s",
                (state, lease.task_id, lease.generation),
            )
            self.emit(
                c,
                lease.task_id,
                lease.generation,
                {"event": "finished", "state": state, "reason": reason},
            )

    def reserve_model(self, lease):
        with self.transaction() as c:
            row = self.guard(c, lease)
            if row["budget_expired"]:
                raise BudgetExceeded("deadline_exceeded")
            c.execute(
                "SELECT COUNT(*) AS n FROM model_calls WHERE task_id=%s AND state IN ('started','unknown')",
                (lease.task_id,),
            )
            if c.fetchone()["n"]:
                raise RuntimeError("model_response_unknown")
            if row["calls_reserved"] >= row["request"]["max_model_calls"]:
                raise BudgetExceeded("model_call_limit")
            n = row["calls_reserved"] + 1
            c.execute(
                "UPDATE repair_tasks SET calls_reserved=%s WHERE id=%s",
                (n, lease.task_id),
            )
            c.execute(
                "INSERT INTO model_calls(task_id,number,generation,state) VALUES(%s,%s,%s,'reserved')",
                (lease.task_id, n, lease.generation),
            )
            self.emit(
                c,
                lease.task_id,
                lease.generation,
                {"event": "model_reserved", "call": n},
            )
            return n

    def model_started(self, lease, number):
        with self.transaction() as c:
            self.guard(c, lease)
            c.execute(
                "UPDATE model_calls SET state='started',updated_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND number=%s AND generation=%s AND state='reserved'",
                (lease.task_id, number, lease.generation),
            )
            if c.rowcount != 1:
                raise RuntimeError("invalid_model_receipt_transition")

    def model_finished(self, lease, number, usage, reason=None):
        if not (
            isinstance(usage, dict)
            and all(
                type(usage.get(key)) is int and usage[key] >= 0
                for key in ("input_tokens", "output_tokens")
            )
        ):
            usage = None
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            c.execute(
                "UPDATE model_calls SET state=%s,usage_json=%s,reason=%s,updated_at=UTC_TIMESTAMP(6) WHERE task_id=%s AND number=%s AND generation=%s AND state='started'",
                (
                    "completed" if usage is not None else "unknown",
                    encoded(usage) if usage is not None else None,
                    reason,
                    lease.task_id,
                    number,
                    lease.generation,
                ),
            )
            if c.rowcount != 1:
                raise RuntimeError("invalid_model_receipt_transition")

    def reserve_candidate(self, lease):
        with self.transaction() as c:
            row = self.guard(c, lease)
            if row["budget_expired"]:
                raise BudgetExceeded("deadline_exceeded")
            if row["candidates_reserved"] >= row["request"]["max_attempts"]:
                raise BudgetExceeded("attempt_limit")
            n = row["candidates_reserved"] + 1
            c.execute(
                "UPDATE repair_tasks SET candidates_reserved=%s WHERE id=%s",
                (n, lease.task_id),
            )
            c.execute(
                "INSERT INTO repair_candidates(task_id,number,generation,state) VALUES(%s,%s,%s,'reserved')",
                (lease.task_id, n, lease.generation),
            )
            return n

    def candidate(self, lease, number, state, *, path=None, sha=None, result=None):
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            c.execute(
                "SELECT state FROM repair_candidates WHERE task_id=%s AND number=%s FOR UPDATE",
                (lease.task_id, number),
            )
            current = c.fetchone()
            allowed = {
                "reserved": {"ready", "rejected"},
                "ready": {"verified"},
                "verified": set(),
                "rejected": set(),
            }
            if current is None or state not in allowed[current["state"]]:
                raise RuntimeError("invalid_candidate_transition")
            c.execute(
                "UPDATE repair_candidates SET state=%s,artifact_path=COALESCE(%s,artifact_path),artifact_sha=COALESCE(%s,artifact_sha),result=%s WHERE task_id=%s AND number=%s",
                (state, path, sha, encoded(result), lease.task_id, number),
            )

    def stage(self, lease, kind, path, sha, metadata):
        with self.transaction() as c:
            self.guard(c, lease)
            c.execute(
                "INSERT INTO stage_artifacts(task_id,generation,kind,artifact_path,artifact_sha,metadata) VALUES(%s,%s,%s,%s,%s,%s)",
                (lease.task_id, lease.generation, kind, path, sha, encoded(metadata)),
            )
            c.execute(
                "UPDATE repair_tasks SET phase=%s WHERE id=%s", (kind, lease.task_id)
            )
            self.emit(
                c,
                lease.task_id,
                lease.generation,
                {"event": "stage_confirmed", "kind": kind, "sha256": sha},
            )

    def test_run(self, lease, kind, path, sha, result, candidate=None):
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            c.execute(
                "INSERT INTO test_runs(id,task_id,generation,candidate_number,kind,artifact_path,artifact_sha,result) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4().hex,
                    lease.task_id,
                    lease.generation,
                    candidate,
                    kind,
                    path,
                    sha,
                    encoded(result),
                ),
            )

    def resource(self, lease, name, state):
        if not re.fullmatch(r"repair-run-[0-9a-f]{16}", name):
            raise ValueError("invalid container identity")
        with self.transaction() as c:
            self.guard(c, lease, allow_cancel=True)
            if state == "reserved":
                c.execute(
                    "INSERT INTO execution_resources(task_id,generation,container_name,state) VALUES(%s,%s,%s,%s)",
                    (lease.task_id, lease.generation, name, state),
                )
            else:
                c.execute(
                    "UPDATE execution_resources SET state=%s WHERE task_id=%s AND container_name=%s",
                    (state, lease.task_id, name),
                )

    def records(self, task_id, table):
        if table not in {
            "model_calls",
            "repair_candidates",
            "stage_artifacts",
            "test_runs",
            "execution_resources",
            "task_attempts",
        }:
            raise ValueError("invalid record table")
        with self.transaction() as c:
            c.execute("SELECT * FROM " + table + " WHERE task_id=%s", (task_id,))
            return [self.decode(r) for r in c.fetchall()]

    def events(self, task_id, after=0, limit=100):
        with self.transaction() as c:
            c.execute(
                "SELECT * FROM task_events WHERE task_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                (task_id, after, limit),
            )
            return [
                {
                    "id": r["sequence"],
                    "created_at": r["created_at"].isoformat() + "Z",
                    "generation": r["generation"],
                    **json.loads(r["payload"]),
                }
                for r in c.fetchall()
            ]

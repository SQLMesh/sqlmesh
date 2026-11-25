# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import typing as t
import uuid

from sqlglot import exp
from sqlmesh.core.state_sync.rewrite import RewriteAttempt, RewriteBlockedError, RewriteStatus
from sqlmesh.utils.date import now_timestamp, TimeLike

if t.TYPE_CHECKING:
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
    from sqlmesh.core.snapshot import Snapshot


class RewriteState:
    """Native state transactions; a claim never expires or permits crash takeover."""

    columns = (
        "attempt_id",
        "owner",
        "name",
        "identifier",
        "version",
        "target",
        "start_ts",
        "end_ts",
        "status",
    )

    def __init__(self, sync: EngineAdapterStateSync):
        self.sync = sync
        self.adapter = sync.engine_adapter
        self.owner_table = exp.table_("_execution_owner", db="sqlmesh_rewrite_prototype")
        self.attempts_table = exp.table_("_rewrite_attempts", db="sqlmesh_rewrite_prototype")

    @contextmanager
    def _durable_transaction(self) -> t.Iterator[None]:
        if self.adapter._connection_pool.is_transaction_active:
            raise RewriteBlockedError(
                "Rewrite protection cannot join a caller-owned state transaction"
            )
        with self.adapter.transaction():
            yield

    @property
    def enabled(self) -> bool:
        return self.adapter.table_exists(self.owner_table)

    def initialize(self) -> None:
        if self.adapter.dialect != "postgres" or not self.adapter.SUPPORTS_TRANSACTIONS:
            raise RewriteBlockedError(
                "Rewrite protection requires native PostgreSQL state transactions"
            )
        owner = self.owner_table.sql(dialect="postgres")
        attempts = self.attempts_table.sql(dialect="postgres")
        with self._durable_transaction():
            self.adapter.execute("CREATE SCHEMA IF NOT EXISTS sqlmesh_rewrite_prototype")
            self.adapter.execute(
                f"CREATE TABLE IF NOT EXISTS {owner} "
                "(id INTEGER PRIMARY KEY CHECK (id=1), token TEXT, state_schema TEXT NOT NULL, warehouse_path TEXT)"
            )
            self.adapter.execute(
                f"CREATE TABLE IF NOT EXISTS {attempts} "
                "(attempt_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, "
                "identifier TEXT NOT NULL, version TEXT NOT NULL, target TEXT NOT NULL, "
                "start_ts BIGINT NOT NULL, end_ts BIGINT NOT NULL CHECK(end_ts>start_ts), "
                "status TEXT NOT NULL CHECK(status IN ('active','terminal','resolved')))"
            )
            schema = exp.convert(self.sync.schema or "").sql(dialect="postgres")
            self.adapter.execute(
                f"INSERT INTO {owner} (id,token,state_schema) VALUES (1,NULL,{schema}) ON CONFLICT DO NOTHING"
            )
            self._owner()

    def bind_warehouse(self, owner: str, path: str) -> None:
        with self._durable_transaction():
            self._validate_owner(owner)
            row = self.adapter.fetchone(
                exp.select("warehouse_path").from_(self.owner_table).where(exp.column("id").eq(1))
            )
            if row[0] not in (None, path):
                raise self.blocked("Protected warehouse routing changed")
            self.adapter.update_table(
                self.owner_table, {"warehouse_path": path}, exp.column("id").eq(1)
            )

    def _owner(self) -> str | None:
        row = self.adapter.fetchone(
            exp.select("token", "state_schema")
            .from_(self.owner_table)
            .where(exp.column("id").eq(1))
            .lock()
        )
        if not row or row[1] != (self.sync.schema or ""):
            raise RewriteBlockedError("Rewrite prototype binds one state namespace per database")
        return row[0]

    def _validate_owner(self, owner: str) -> None:
        if self._owner() != owner:
            raise RewriteBlockedError("Stale or absent native execution owner")

    def check_owner(self, owner: str) -> None:
        with self._durable_transaction():
            self._validate_owner(owner)

    def check_attempt(self, a: RewriteAttempt) -> None:
        with self._durable_transaction():
            self._validate_owner(a.owner)
            if (
                self._read(exp.column("attempt_id").eq(a.attempt_id)) != [a]
                or a.status != RewriteStatus.ACTIVE
            ):
                raise RewriteBlockedError("Evaluation lacks an exact active rewrite attempt")

    def list_unresolved(self) -> list[RewriteAttempt]:
        return self._read(exp.column("status").neq(RewriteStatus.RESOLVED.value))

    def _read(self, where: exp.Expr) -> list[RewriteAttempt]:
        rows = self.adapter.fetchall(
            exp.select(*self.columns)
            .from_(self.attempts_table)
            .where(where)
            .order_by("start_ts", "attempt_id")
        )
        return [RewriteAttempt(*r[:-1], RewriteStatus(r[-1])) for r in rows]

    def blocked(self, message: str) -> RewriteBlockedError:
        bounds = ", ".join(
            f"{a.name} [{a.start}, {a.end}) {a.status.value}" for a in self.list_unresolved()
        )
        return RewriteBlockedError(f"{message}; unresolved rewrites: {bounds or 'none recorded'}")

    def claim(self, owner: str) -> None:
        with self._durable_transaction():
            current = self._owner()
            if current not in (None, owner):
                raise self.blocked("Native executor is held; no timeout or automatic adoption")
            if current is None and any(
                a.status == RewriteStatus.ACTIVE for a in self.list_unresolved()
            ):
                raise self.blocked("Unknown execution activity")
            self.adapter.update_table(self.owner_table, {"token": owner}, exp.column("id").eq(1))
            # Only terminal records can cross invocations. Their old local SQL call returned,
            # and release occurred after the scheduler joined every worker.
            self.adapter.update_table(
                self.attempts_table,
                {"owner": owner},
                exp.column("status").eq(RewriteStatus.TERMINAL.value),
            )

    def release(self, owner: str) -> None:
        with self._durable_transaction():
            self._validate_owner(owner)
            if any(a.status == RewriteStatus.ACTIVE for a in self.list_unresolved()):
                return  # Durable hold after uncertain SQL, completion response loss, or process death.
            self.adapter.update_table(self.owner_table, {"token": None}, exp.column("id").eq(1))

    @contextmanager
    def execution(self, owner: str) -> t.Iterator[None]:
        self.claim(owner)
        try:
            yield
        finally:
            self.release(owner)

    def begin(
        self,
        owner: str,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        attempt_id: str | None = None,
    ) -> RewriteAttempt:
        if not snapshot.version:
            raise RewriteBlockedError("Rewrite requires a versioned native snapshot")
        start, end = snapshot.inclusive_exclusive(start, end, strict=False, expand=False)
        a = RewriteAttempt(
            attempt_id or str(uuid.uuid4()),
            owner,
            snapshot.name,
            snapshot.identifier,
            snapshot.version,
            snapshot.table_name(),
            start,
            end,
            RewriteStatus.ACTIVE,
        )
        with self._durable_transaction():
            self._validate_owner(owner)
            prior = self._read(exp.column("attempt_id").eq(a.attempt_id))
            if prior:
                if (
                    replace(prior[0], status=RewriteStatus.ACTIVE) != a
                    or prior[0].status == RewriteStatus.RESOLVED
                ):
                    raise RewriteBlockedError("Unknown or changed rewrite request identity")
                self.adapter.update_table(
                    self.attempts_table,
                    {"status": RewriteStatus.ACTIVE.value},
                    exp.column("attempt_id").eq(a.attempt_id),
                )
            else:
                overlap = [
                    p
                    for p in self.list_unresolved()
                    if p.target == a.target and p.start < a.end and a.start < p.end
                ]
                if overlap:
                    raise self.blocked("Another unresolved rewrite overlaps this batch")
                values = (
                    a.attempt_id,
                    a.owner,
                    a.name,
                    a.identifier,
                    a.version,
                    a.target,
                    a.start,
                    a.end,
                    a.status.value,
                )
                query = exp.select(
                    *[exp.alias_(exp.convert(v), c) for c, v in zip(self.columns, values)]
                )
                self.adapter.insert_append(
                    self.attempts_table,
                    query,
                    target_columns_to_types={
                        c: exp.DataType.build("bigint" if c.endswith("_ts") else "text")
                        for c in self.columns
                    },
                )
        return a

    def terminal(self, a: RewriteAttempt) -> None:
        with self._durable_transaction():
            self._validate_owner(a.owner)
            if self._read(exp.column("attempt_id").eq(a.attempt_id)) != [a]:
                raise RewriteBlockedError("Stale terminal rewrite record")
            self.adapter.update_table(
                self.attempts_table,
                {"status": RewriteStatus.TERMINAL.value},
                exp.column("attempt_id").eq(a.attempt_id),
            )

    def finish(self, a: RewriteAttempt, snapshot: Snapshot) -> None:
        with self._durable_transaction():
            self._validate_owner(a.owner)
            prior = self._read(exp.column("attempt_id").eq(a.attempt_id))
            if not prior or replace(prior[0], status=RewriteStatus.ACTIVE) != a:
                raise RewriteBlockedError("Stale or unknown rewrite completion")
            if prior[0].status == RewriteStatus.RESOLVED:
                return  # Exact receipt, no second native interval event.
            if prior[0].status != RewriteStatus.ACTIVE:
                raise RewriteBlockedError("Terminal rewrite must be begun again before completion")
            bounds = snapshot.inclusive_exclusive(a.start, a.end, strict=False, expand=False)
            if (
                snapshot.name,
                snapshot.identifier,
                snapshot.version,
                snapshot.table_name(),
                *bounds,
            ) != (a.name, a.identifier, a.version, a.target, a.start, a.end):
                raise RewriteBlockedError("Completion does not match native snapshot and bounds")
            self.sync.add_interval(
                snapshot, a.start, a.end, is_dev=False, last_altered_ts=now_timestamp()
            )
            self.adapter.update_table(
                self.attempts_table,
                {"status": RewriteStatus.RESOLVED.value},
                exp.column("attempt_id").eq(a.attempt_id),
            )

    def reject_lifecycle(self) -> None:
        if self.enabled:
            raise self.blocked(
                "State lifecycle and plans are unsupported while prototype protection is initialized"
            )

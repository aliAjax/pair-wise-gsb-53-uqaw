"""SQLite 表结构、整批事务与查询。"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()
        self._backfill_legacy_batches()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'ready',
                    quota_date TEXT NOT NULL,
                    quota_limit INTEGER,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    basis_version INTEGER,
                    review_status TEXT NOT NULL DEFAULT '',
                    main_case_id INTEGER REFERENCES records(id),
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, reference)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    batch_version INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    receipt_number TEXT NOT NULL UNIQUE,
                    issued_day INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_receipt_cases (
                    receipt_id INTEGER NOT NULL REFERENCES case_receipts(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    PRIMARY KEY(receipt_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    quota_date TEXT NOT NULL,
                    receipt_number TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS batch_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, version)
                );
                CREATE TABLE IF NOT EXISTS save_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    save_token TEXT NOT NULL,
                    committed_version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, save_token)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_quota_date ON quota_reservations(quota_date);
                CREATE INDEX IF NOT EXISTS idx_batch_audit_batch ON batch_audit_events(batch_id, id);
                CREATE INDEX IF NOT EXISTS idx_batch_snapshots ON batch_snapshots(batch_id, version);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_save_tokens_global ON save_tokens(save_token);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            alter_sql = {
                "batch_id": "ALTER TABLE records ADD COLUMN batch_id INTEGER REFERENCES batches(id)",
                "basis_version": "ALTER TABLE records ADD COLUMN basis_version INTEGER",
                "review_status": "ALTER TABLE records ADD COLUMN review_status TEXT NOT NULL DEFAULT ''",
                "main_case_id": "ALTER TABLE records ADD COLUMN main_case_id INTEGER REFERENCES records(id)",
            }
            for name, sql in alter_sql.items():
                if name not in columns:
                    connection.execute(sql)
            audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)")}
            if "batch_id" not in audit_columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN batch_id INTEGER REFERENCES batches(id)")
            if "batch_version" not in audit_columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN batch_version INTEGER")
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone()
        if schema and "REFERENCE TEXT NOT NULL UNIQUE" in schema["sql"].upper():
            self._rebuild_records_with_batch_uniqueness()
        with self._connect() as connection:
            connection.execute("CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id)")

    def _rebuild_records_with_batch_uniqueness(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE records RENAME TO records_old")
            connection.execute(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    basis_version INTEGER,
                    review_status TEXT NOT NULL DEFAULT '',
                    main_case_id INTEGER REFERENCES records(id),
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, reference)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO records(id,batch_id,reference,state,version,payload,basis_version,review_status,
                                    main_case_id,created_by,updated_by,created_at,updated_at)
                SELECT id,batch_id,reference,state,version,payload,basis_version,review_status,
                       main_case_id,created_by,updated_by,created_at,updated_at
                FROM records_old
                """
            )
            connection.execute("DROP TABLE records_old")
            connection.execute("CREATE INDEX idx_records_state ON records(state)")
            connection.execute("CREATE INDEX idx_records_batch ON records(batch_id)")
            connection.commit()
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if item.get("quota_limit") is not None:
            item["quota_limit"] = int(item["quota_limit"])
        item["version"] = int(item["version"])
        return item

    def _snapshot(self, connection: sqlite3.Connection, batch_id: int) -> Dict[str, Any]:
        batch = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise NotFound("批次不存在")
        cases = []
        record_id_to_ref: Dict[int, str] = {}
        for row in connection.execute("SELECT * FROM records WHERE batch_id=? ORDER BY id", (batch_id,)):
            item = self._row(row)
            record_id_to_ref[item["id"]] = item["reference"]
            main_ref = record_id_to_ref.get(item["main_case_id"])
            if main_ref is None and item["main_case_id"] is not None:
                main = connection.execute("SELECT reference FROM records WHERE id=?", (item["main_case_id"],)).fetchone()
                main_ref = main["reference"] if main else None
            item["main_reference"] = main_ref
            cases.append(item)
        receipts = []
        receipt_rows = connection.execute(
            """
            SELECT r.*, GROUP_CONCAT(rc.record_id) AS record_ids
            FROM case_receipts r
            LEFT JOIN case_receipt_cases rc ON r.id = rc.receipt_id
            WHERE r.batch_id=?
            GROUP BY r.id
            ORDER BY r.id
            """,
            (batch_id,),
        ).fetchall()
        for row in receipt_rows:
            item = dict(row)
            item["payload"] = _loads(item["payload"], {})
            ids = [int(value) for value in (item.pop("record_ids") or "").split(",") if value]
            item["case_references"] = [record_id_to_ref[item_id] for item_id in ids if item_id in record_id_to_ref]
            item["version"] = int(item["version"])
            receipts.append(item)
        reservations = connection.execute(
            "SELECT receipt_number FROM quota_reservations WHERE batch_id=? AND quota_date=? ORDER BY receipt_number",
            (batch_id, batch["quota_date"]),
        ).fetchall()
        result = self._batch_row(batch)
        result["cases"] = cases
        result["receipts"] = receipts
        result["quota"] = {
            "date": batch["quota_date"],
            "limit": None if batch["quota_limit"] is None else int(batch["quota_limit"]),
            "reserved": len(reservations),
            "receipt_numbers": [row["receipt_number"] for row in reservations],
        }
        return result

    def _persist_snapshot(self, connection: sqlite3.Connection, batch_id: int, version: int, actor_id: str, now: str) -> Dict[str, Any]:
        snapshot = self._snapshot(connection, batch_id)
        connection.execute(
            "INSERT INTO batch_snapshots(batch_id,version,snapshot,created_by,created_at) VALUES(?,?,?,?,?)",
            (batch_id, version, _dumps(snapshot), actor_id, now),
        )
        return snapshot

    def _backfill_legacy_batches(self) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            legacy = connection.execute("SELECT * FROM records WHERE batch_id IS NULL ORDER BY id").fetchall()
            for row in legacy:
                created_by = (row["created_by"] or "").strip()
                created_at = (row["created_at"] or "").strip()
                complete = bool(created_by and self._date_part(created_at))
                status = "ready" if complete else "pending_verification"
                state = row["state"] if complete else "pending_verification"
                batch_no = ("LEGACY-%08d" if complete else "PENDING-%08d") % int(row["id"])
                version = int(row["version"])
                cursor = connection.execute(
                    """
                    INSERT INTO batches(batch_no,version,status,quota_date,quota_limit,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_no,
                        version,
                        status,
                        self._date_part(created_at) or now[:10],
                        None,
                        created_by,
                        row["updated_by"] or created_by,
                        created_at or now,
                        row["updated_at"] or created_at or now,
                    ),
                )
                batch_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    UPDATE records
                    SET batch_id=?, state=?, basis_version=?, review_status=?, updated_at=?
                    WHERE id=?
                    """,
                    (batch_id, state, 1 if complete else None, "" if complete else "pending_verification", row["updated_at"] or now, row["id"]),
                )
                connection.execute(
                    "UPDATE audit_events SET batch_id=? WHERE record_id=?",
                    (batch_id, row["id"]),
                )
                action = "legacy_batch_backfilled" if complete else "legacy_batch_pending_verification"
                details = {
                    "original_owner": created_by,
                    "original_date": created_at,
                    "reason": "历史数据缺少批次号" if not complete else "历史数据按原承办人和日期回填批次",
                }
                connection.execute(
                    """
                    INSERT INTO batch_audit_events(batch_id,record_id,action,actor_id,version,details,event_key,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (batch_id, row["id"], action, created_by or "system", version, _dumps(details), "%s:%s" % (action, row["id"]), now),
                )
                self._persist_snapshot(connection, batch_id, version, created_by or "system", now)

    @staticmethod
    def _date_part(value: str) -> str:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except (TypeError, ValueError):
            return ""

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        batch_no = "B-" + uuid.uuid4().hex[:12].upper()
        quota_date = now[:10]
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO batches(batch_no,version,status,quota_date,quota_limit,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (batch_no, 1, "ready", quota_date, None, actor_id, actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                cursor = connection.execute(
                    """
                    INSERT INTO records(batch_id,reference,state,version,payload,basis_version,review_status,
                                        main_case_id,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (batch_id, reference, state, 1, _dumps(payload), 1, "", None, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO audit_events(record_id,batch_id,action,actor_id,version,batch_version,details,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (record_id, batch_id, "created", actor_id, 1, 1, _dumps({"state": state}), now),
                )
                self._persist_snapshot(connection, batch_id, 1, actor_id, now)
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            except sqlite3.IntegrityError as exc:
                raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_by_reference(self, batch_id: int, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE batch_id=? AND reference=?", (batch_id, reference)
            ).fetchone()
        return self._row(row) if row else None

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def get_batch_by_no(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                raise NotFound("批次不存在")
            return self._snapshot(connection, int(row["id"]))

    def get_committed_save(self, batch_no: str, save_token: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT b.batch_no, t.committed_version, t.snapshot
                FROM save_tokens t JOIN batches b ON b.id=t.batch_id
                WHERE b.batch_no=? AND t.save_token=?
                """,
                (batch_no, save_token),
            ).fetchone()
        if row is None:
            return None
        return {"batch_no": row["batch_no"], "version": int(row["committed_version"]), "snapshot": _loads(row["snapshot"], {}), "replayed": True}

    def get_committed_save_token(self, save_token: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT b.batch_no, t.committed_version, t.snapshot
                FROM save_tokens t JOIN batches b ON b.id=t.batch_id
                WHERE t.save_token=?
                """,
                (save_token,),
            ).fetchone()
        if row is None:
            return None
        return {"batch_no": row["batch_no"], "version": int(row["committed_version"]), "snapshot": _loads(row["snapshot"], {}), "replayed": True}

    def create_batch(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        actor_id = plan["actor_id"]
        now = _now()
        batch_no = "B-" + uuid.uuid4().hex[:12].upper()
        target_version = 1
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO batches(batch_no,version,status,quota_date,quota_limit,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_no,
                        target_version,
                        "ready",
                        plan["quota_date"],
                        plan.get("quota_limit"),
                        actor_id,
                        actor_id,
                        now,
                        now,
                    ),
                )
                batch_id = int(cursor.lastrowid)
                id_by_ref: Dict[str, int] = {}
                for case in plan["cases"]:
                    cursor = connection.execute(
                        """
                        INSERT INTO records(batch_id,reference,state,version,payload,basis_version,review_status,
                                            main_case_id,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id,
                            case["reference"],
                            case["state"],
                            target_version,
                            _dumps(case["payload"]),
                            case["basis_version"],
                            case.get("review_status", ""),
                            None,
                            actor_id,
                            actor_id,
                            now,
                            now,
                        ),
                    )
                    id_by_ref[case["reference"]] = int(cursor.lastrowid)
                for case in plan["cases"]:
                    main_id = id_by_ref.get(case.get("main_reference")) if case.get("main_reference") else None
                    connection.execute(
                        "UPDATE records SET main_case_id=? WHERE id=?",
                        (main_id, id_by_ref[case["reference"]]),
                    )

                receipt_id_by_number: Dict[str, int] = {}
                for receipt in plan["receipts"]:
                    cursor = connection.execute(
                        """
                        INSERT INTO case_receipts(batch_id,receipt_number,issued_day,payload,version,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id,
                            receipt["receipt_number"],
                            receipt["issued_day"],
                            _dumps(receipt["payload"]),
                            target_version,
                            actor_id,
                            actor_id,
                            now,
                            now,
                        ),
                    )
                    receipt_id_by_number[receipt["receipt_number"]] = int(cursor.lastrowid)
                    for ref in receipt["case_references"]:
                        connection.execute(
                            "INSERT OR IGNORE INTO case_receipt_cases(receipt_id,record_id) VALUES(?,?)",
                            (receipt_id_by_number[receipt["receipt_number"]], id_by_ref[ref]),
                        )

                existing_today = connection.execute(
                    "SELECT COUNT(*) AS total FROM quota_reservations WHERE quota_date=?",
                    (plan["quota_date"],),
                ).fetchone()["total"]
                if plan.get("quota_limit") is not None and int(existing_today) + len(plan["receipts"]) > int(plan["quota_limit"]):
                    connection.rollback()
                    raise Conflict("当日受理名额不足", {"reserved": int(existing_today), "limit": int(plan["quota_limit"])})
                for receipt in plan["receipts"]:
                    connection.execute(
                        """
                        INSERT INTO quota_reservations(batch_id,quota_date,receipt_number,created_by,created_at)
                        VALUES(?,?,?,?,?)
                        """,
                        (batch_id, plan["quota_date"], receipt["receipt_number"], actor_id, now),
                    )

                for event in plan["events"]:
                    record_id = id_by_ref.get(event.get("case_reference")) if event.get("case_reference") else None
                    connection.execute(
                        """
                        INSERT INTO batch_audit_events(batch_id,record_id,action,actor_id,version,details,event_key,created_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id,
                            record_id,
                            event["action"],
                            actor_id,
                            target_version,
                            _dumps(event.get("details", {})),
                            event["event_key"],
                            now,
                        ),
                    )

                snapshot = self._persist_snapshot(connection, batch_id, target_version, actor_id, now)
                connection.execute(
                    """
                    INSERT INTO save_tokens(batch_id,save_token,committed_version,snapshot,created_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (batch_id, plan["save_token"], target_version, _dumps(snapshot), now),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("批次数据存在唯一约束冲突") from exc
        return {"batch_no": batch_no, "version": target_version, "snapshot": snapshot, "replayed": False}

    def _get_batch_for_update(self, connection: sqlite3.Connection, batch_no: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return row

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT r.*, b.batch_no, b.status AS batch_status
                FROM records r JOIN batches b ON b.id = r.batch_id
                WHERE r.id=?
                """,
                (record_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["batch_status"] == "pending_verification":
                connection.rollback()
                raise Conflict("待核历史案件不能继续办理")
            batch_id = int(row["batch_id"])
            batch = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if int(batch["version"]) != int(expected_version):
                snapshot = self._snapshot(connection, batch_id)
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试", {"current_version": int(batch["version"]), "snapshot": snapshot})
            version = int(expected_version) + 1
            review_status = row["review_status"]
            basis_version = row["basis_version"]
            connection.execute(
                """
                UPDATE records
                SET state=?,version=?,payload=?,basis_version=?,review_status=?,updated_by=?,updated_at=?
                WHERE id=?
                """,
                (state, version, _dumps(payload), basis_version, review_status, actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE records SET version=?, updated_by=?, updated_at=? WHERE batch_id=? AND id<>?",
                (version, actor_id, now, batch_id, record_id),
            )
            connection.execute(
                "UPDATE case_receipts SET version=? WHERE batch_id=?",
                (version, batch_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events(record_id,batch_id,action,actor_id,version,batch_version,details,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (record_id, batch_id, action, actor_id, version, version, _dumps(details), now),
            )
            connection.execute(
                "UPDATE batches SET version=?,updated_by=?,updated_at=? WHERE id=?",
                (version, actor_id, now, batch_id),
            )
            self._persist_snapshot(connection, batch_id, version, actor_id, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def save_batch(self, batch_no: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        actor_id = plan["actor_id"]
        expected_version = int(plan["expected_version"])
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._get_batch_for_update(connection, batch_no)
            batch_id = int(batch["id"])
            token_row = connection.execute(
                "SELECT * FROM save_tokens WHERE batch_id=? AND save_token=?",
                (batch_id, plan["save_token"]),
            ).fetchone()
            if token_row is not None:
                snapshot = _loads(token_row["snapshot"], {})
                connection.commit()
                return {"version": int(token_row["committed_version"]), "snapshot": snapshot, "replayed": True}
            current_version = int(batch["version"])
            if current_version != expected_version:
                snapshot = self._snapshot(connection, batch_id)
                connection.rollback()
                raise Conflict(
                    "批次版本已前进，请保留填写内容后基于新版本重试",
                    {"current_version": current_version, "snapshot": snapshot},
                )
            if batch["quota_date"] != plan["quota_date"]:
                connection.rollback()
                raise Conflict("批次办理日期不能修改", {"current_version": current_version})

            target_version = current_version + 1
            existing_refs = {
                row["reference"]: dict(row)
                for row in connection.execute("SELECT * FROM records WHERE batch_id=?", (batch_id,)).fetchall()
            }
            if set(existing_refs) != {case["reference"] for case in plan["cases"] if case["exists"]}:
                connection.rollback()
                raise Conflict("完整批次必须包含批次内全部既有案件", {"current_version": current_version})

            id_by_ref: Dict[str, int] = {ref: int(row["id"]) for ref, row in existing_refs.items()}
            for case in plan["cases"]:
                payload = _dumps(case["payload"])
                if case["exists"]:
                    connection.execute(
                        """
                        UPDATE records
                        SET state=?,version=?,payload=?,basis_version=?,review_status=?,updated_by=?,updated_at=?
                        WHERE id=?
                        """,
                        (
                            case["state"],
                            target_version,
                            payload,
                            case["basis_version"],
                            case.get("review_status", ""),
                            actor_id,
                            now,
                            id_by_ref[case["reference"]],
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO records(batch_id,reference,state,version,payload,basis_version,review_status,
                                            main_case_id,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id,
                            case["reference"],
                            case["state"],
                            target_version,
                            payload,
                            case["basis_version"],
                            case.get("review_status", ""),
                            None,
                            actor_id,
                            actor_id,
                            now,
                            now,
                        ),
                    )
                    id_by_ref[case["reference"]] = int(cursor.lastrowid)
            for case in plan["cases"]:
                main_ref = case.get("main_reference")
                main_id = id_by_ref.get(main_ref) if main_ref else None
                connection.execute(
                    "UPDATE records SET main_case_id=? WHERE batch_id=? AND reference=?",
                    (main_id, batch_id, case["reference"]),
                )

            existing_receipts = {
                row["receipt_number"]: dict(row)
                for row in connection.execute("SELECT * FROM case_receipts WHERE batch_id=?", (batch_id,)).fetchall()
            }
            receipt_id_by_number: Dict[str, int] = {
                number: int(row["id"]) for number, row in existing_receipts.items()
            }
            for receipt in plan["receipts"]:
                number = receipt["receipt_number"]
                foreign = connection.execute(
                    "SELECT batch_id FROM case_receipts WHERE receipt_number=? AND batch_id<>?",
                    (number, batch_id),
                ).fetchone()
                if foreign is not None:
                    connection.rollback()
                    raise Conflict("回执编号不能跨批次重复：%s" % number)
                if number in existing_receipts:
                    connection.execute(
                        "UPDATE case_receipts SET issued_day=?,payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                        (
                            receipt["issued_day"],
                            _dumps(receipt["payload"]),
                            target_version,
                            actor_id,
                            now,
                            receipt_id_by_number[number],
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO case_receipts(batch_id,receipt_number,issued_day,payload,version,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id,
                            number,
                            receipt["issued_day"],
                            _dumps(receipt["payload"]),
                            target_version,
                            actor_id,
                            actor_id,
                            now,
                            now,
                        ),
                    )
                    receipt_id_by_number[number] = int(cursor.lastrowid)
                for ref in receipt["case_references"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO case_receipt_cases(receipt_id,record_id) VALUES(?,?)",
                        (receipt_id_by_number[number], id_by_ref[ref]),
                    )

            reservations = connection.execute(
                "SELECT receipt_number FROM quota_reservations WHERE batch_id=? AND quota_date=?",
                (batch_id, plan["quota_date"]),
            ).fetchall()
            reserved_numbers = {row["receipt_number"] for row in reservations}
            new_reservations = []
            for receipt in plan["receipts"]:
                if receipt["receipt_number"] not in existing_receipts and receipt["receipt_number"] not in reserved_numbers:
                    new_reservations.append(receipt["receipt_number"])
                    reserved_numbers.add(receipt["receipt_number"])
            total_reserved = connection.execute(
                "SELECT COUNT(*) AS total FROM quota_reservations WHERE quota_date=?",
                (plan["quota_date"],),
            ).fetchone()["total"]
            if plan["quota_limit"] is not None and int(total_reserved) + len(new_reservations) > int(plan["quota_limit"]):
                connection.rollback()
                raise Conflict("当日受理名额不足", {"reserved": int(total_reserved), "limit": int(plan["quota_limit"])})
            for number in new_reservations:
                connection.execute(
                    """
                    INSERT INTO quota_reservations(batch_id,quota_date,receipt_number,created_by,created_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (batch_id, plan["quota_date"], number, actor_id, now),
                )

            for event in plan["events"]:
                record_id = id_by_ref.get(event.get("case_reference")) if event.get("case_reference") else None
                connection.execute(
                    """
                    INSERT INTO batch_audit_events(batch_id,record_id,action,actor_id,version,details,event_key,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id,
                        record_id,
                        event["action"],
                        actor_id,
                        target_version,
                        _dumps(event.get("details", {})),
                        event["event_key"],
                        now,
                    ),
                )

            connection.execute(
                "UPDATE batches SET version=?,quota_limit=?,updated_by=?,updated_at=? WHERE id=?",
                (target_version, plan.get("quota_limit"), actor_id, now, batch_id),
            )
            snapshot = self._persist_snapshot(connection, batch_id, target_version, actor_id, now)
            connection.execute(
                """
                INSERT INTO save_tokens(batch_id,save_token,committed_version,snapshot,created_at)
                VALUES(?,?,?,?,?)
                """,
                (batch_id, plan["save_token"], target_version, _dumps(snapshot), now),
            )
            connection.commit()
        return {"version": target_version, "snapshot": snapshot, "replayed": False}

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version,batch_id FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                """
                INSERT INTO audit_events(record_id,batch_id,action,actor_id,version,batch_version,details,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    row["batch_id"],
                    action,
                    actor_id,
                    int(row["version"]),
                    int(row["version"]),
                    _dumps(details),
                    _now(),
                ),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            old_rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
            batch_rows = connection.execute(
                "SELECT * FROM batch_audit_events WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        result = []
        for row in old_rows:
            item = dict(row)
            item["event_id"] = "record:%s" % item.pop("id")
            item["kind"] = "record"
            item["details"] = _loads(item["details"])
            result.append(item)
        for row in batch_rows:
            item = dict(row)
            item["event_id"] = "batch:%s" % item.pop("id")
            item["kind"] = "batch"
            item["batch_version"] = item["version"]
            item["details"] = _loads(item["details"])
            result.append(item)
        return sorted(result, key=lambda item: (item["created_at"], item["event_id"]))

    def batch_timeline(self, batch_no: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            batch = connection.execute("SELECT id FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if batch is None:
                raise NotFound("批次不存在")
            batch_id = int(batch["id"])
            old_rows = connection.execute(
                """
                SELECT e.*, r.reference AS case_reference
                FROM audit_events e JOIN records r ON r.id=e.record_id
                WHERE e.batch_id=?
                ORDER BY e.id
                """,
                (batch_id,),
            ).fetchall()
            batch_rows = connection.execute(
                """
                SELECT e.*, r.reference AS case_reference
                FROM batch_audit_events e LEFT JOIN records r ON r.id=e.record_id
                WHERE e.batch_id=?
                ORDER BY e.id
                """,
                (batch_id,),
            ).fetchall()
        result = []
        for row in old_rows:
            item = dict(row)
            item["event_id"] = "record:%s" % item.pop("id")
            item["kind"] = "record"
            item["batch_version"] = item.pop("batch_version", None)
            item["details"] = _loads(item["details"])
            result.append(item)
        for row in batch_rows:
            item = dict(row)
            item["event_id"] = "batch:%s" % item.pop("id")
            item["kind"] = "batch"
            item["batch_version"] = item["version"]
            item["details"] = _loads(item["details"])
            result.append(item)
        return sorted(result, key=lambda item: (item["created_at"], item["event_id"]))

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

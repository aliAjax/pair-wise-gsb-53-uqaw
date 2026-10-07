"""SQLite 表结构、批次事务与查询。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


LEGACY_BATCH_PREFIX = "LEGACY-"
BATCH_NO_SYSTEM = "SYSTEM"
META_BACKFILL = "legacy_backfill"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tx:
    """BEGIN IMMEDIATE 下的加锁事务句柄，业务编排通过它读写同一批次版本。"""

    def __init__(self, repository: "Repository", connection: sqlite3.Connection) -> None:
        self.repository = repository
        self.connection = connection
        self._idem_audit: Dict[str, int] = {}

    # ---------------- 读取（已持写锁，读到的即为本次落盘依据） ----------------
    def get_batch_locked(self, batch_no: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        return dict(row) if row is not None else None

    def get_save(self, batch_no: str, save_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM batch_saves WHERE batch_no=? AND save_id=?", (batch_no, save_id)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_member(self, reference: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return self.repository._row(row) if row is not None else None

    def list_members(self, batch_no: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM records WHERE batch_no=? ORDER BY id", (batch_no,)
        ).fetchall()
        return [self.repository._row(row) for row in rows]

    def get_receipt(self, receipt_no: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["documents"] = json.loads(item["documents"])
        return item

    def list_receipts(self, batch_no: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM receipts WHERE batch_no=? ORDER BY id", (batch_no,)
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["documents"] = json.loads(item["documents"])
            items.append(item)
        return items

    def list_quota(self, batch_no: str) -> List[Dict[str, int]]:
        rows = self.connection.execute(
            "SELECT day, SUM(amount) AS amount FROM quota_allocations WHERE batch_no=? GROUP BY day ORDER BY day",
            (batch_no,),
        ).fetchall()
        return [{"day": int(row["day"]), "amount": int(row["amount"])} for row in rows]

    def quota_used(self, day: int) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS used FROM quota_allocations WHERE day=?", (day,)
        ).fetchone()
        return int(row["used"])

    # ---------------- 写入 ----------------
    def insert_member(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, batch_no: str, batch_version: int, now: str) -> Dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,batch_no,batch_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             actor_id, actor_id, now, now, batch_no, batch_version),
        )
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return self.repository._row(row)

    def update_member(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, batch_version: int, now: str) -> Dict[str, Any]:
        row = self.connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        new_version = int(expected_version) + 1
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=?,batch_version=? WHERE id=?",
            (state, new_version, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             actor_id, now, batch_version, record_id),
        )
        result = self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return self.repository._row(result)

    def insert_receipt(self, receipt_no: str, documents: List[str], note: str, actor_id: str, batch_no: str, batch_version: int, now: str) -> Dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO receipts(receipt_no,documents,note,created_by,updated_by,created_at,updated_at,first_batch_no,batch_no,batch_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (receipt_no, json.dumps(documents, ensure_ascii=False), note, actor_id, actor_id,
             now, now, batch_no, batch_no, batch_version),
        )
        row = self.connection.execute("SELECT * FROM receipts WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        item = dict(row)
        item["documents"] = json.loads(item["documents"])
        return item

    def update_receipt(self, receipt_id: int, documents: List[str], note: str, actor_id: str, batch_no: str, batch_version: int, now: str) -> Dict[str, Any]:
        self.connection.execute(
            "UPDATE receipts SET documents=?,note=?,updated_by=?,updated_at=?,batch_no=?,batch_version=? WHERE id=?",
            (json.dumps(documents, ensure_ascii=False), note, actor_id, now, batch_no, batch_version, receipt_id),
        )
        row = self.connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        item = dict(row)
        item["documents"] = json.loads(item["documents"])
        return item

    def allocate_quota(self, day: int, amount: int, batch_no: str, save_id: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO quota_allocations(day,amount,batch_no,save_id,created_at) VALUES(?,?,?,?,?)",
            (day, amount, batch_no, save_id, now),
        )

    def audit(self, record_id: int, action: str, actor_id: str, version: int, batch_no: Optional[str], batch_version: Optional[int], details: Dict[str, Any], idem_key: str = None, now: str = None) -> None:
        if idem_key is not None:
            if idem_key in self._idem_audit:
                return
            existing = self.connection.execute(
                "SELECT 1 FROM audit_events WHERE idem_key=? LIMIT 1", (idem_key,)
            ).fetchone()
            if existing is not None:
                self._idem_audit[idem_key] = 1
                return
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version,idem_key)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (record_id, action, actor_id, version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), now or _now(),
             batch_no, batch_version, idem_key),
        )
        if idem_key is not None:
            self._idem_audit[idem_key] = 1

    def advance_batch(self, batch_no: str, version: int, actor_id: str, snapshot: Dict[str, Any], save_id: str, now: str) -> None:
        self.connection.execute(
            "UPDATE batches SET version=?,updated_by=?,updated_at=?,last_save_id=?,snapshot=? WHERE batch_no=?",
            (version, actor_id, now, save_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), batch_no),
        )

    def record_save(self, batch_no: str, save_id: str, expected_version: int, new_version: int, actor_id: str, request: Dict[str, Any], result: Dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO batch_saves(batch_no,save_id,expected_version,new_version,actor_id,request,result,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (batch_no, save_id, expected_version, new_version, actor_id,
             json.dumps(request, ensure_ascii=False, sort_keys=True),
             json.dumps(result, ensure_ascii=False, sort_keys=True), now),
        )


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS batches (
                    batch_no TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    quota_per_day INTEGER NOT NULL DEFAULT 50,
                    last_save_id TEXT,
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    documents TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    first_batch_no TEXT NOT NULL,
                    batch_no TEXT NOT NULL,
                    batch_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    batch_no TEXT NOT NULL,
                    save_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_no, save_id, day)
                );
                CREATE INDEX IF NOT EXISTS idx_quota_day ON quota_allocations(day);
                CREATE TABLE IF NOT EXISTS batch_saves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    save_id TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    new_version INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_no, save_id)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._add_columns(connection)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_idem ON audit_events(idem_key) WHERE idem_key IS NOT NULL"
            )

    @staticmethod
    def _add_columns(connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
        if "batch_no" not in existing:
            connection.execute("ALTER TABLE records ADD COLUMN batch_no TEXT")
        if "batch_version" not in existing:
            connection.execute("ALTER TABLE records ADD COLUMN batch_version INTEGER")
        existing_audit = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)")}
        if "batch_no" not in existing_audit:
            connection.execute("ALTER TABLE audit_events ADD COLUMN batch_no TEXT")
        if "batch_version" not in existing_audit:
            connection.execute("ALTER TABLE audit_events ADD COLUMN batch_version INTEGER")
        if "idem_key" not in existing_audit:
            connection.execute("ALTER TABLE audit_events ADD COLUMN idem_key TEXT")

    @contextmanager
    def transaction(self):
        """以 BEGIN IMMEDIATE 打开写事务，整批保存要么全部落盘，要么全部回滚。"""
        connection = self._connect()
        tx = Tx(self, connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield tx
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------
    def create_batch(self, batch_no: str, application_id: str, quota_per_day: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        snapshot = {"batch_no": batch_no, "application_id": application_id, "version": 0,
                    "members": [], "receipts": [], "quota": []}
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO batches(batch_no,application_id,version,quota_per_day,snapshot,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch_no, application_id, 0, quota_per_day,
                     json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now),
                )
                row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次号已存在") from exc
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        return item

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        return item

    def list_batches(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batches ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["snapshot"] = json.loads(item["snapshot"])
        return items

    def batch_detail(self, batch_no: str) -> Dict[str, Any]:
        batch = self.get_batch(batch_no)
        with self._connect() as connection:
            member_rows = connection.execute(
                "SELECT * FROM records WHERE batch_no=? ORDER BY id", (batch_no,)
            ).fetchall()
            receipt_rows = connection.execute(
                "SELECT * FROM receipts WHERE batch_no=? ORDER BY id", (batch_no,)
            ).fetchall()
            quota_rows = connection.execute(
                "SELECT day, SUM(amount) AS amount FROM quota_allocations WHERE batch_no=? GROUP BY day ORDER BY day",
                (batch_no,),
            ).fetchall()
        members = [self._row(row) for row in member_rows]
        receipts = []
        for row in receipt_rows:
            item = dict(row)
            item["documents"] = json.loads(item["documents"])
            receipts.append(item)
        batch["members"] = members
        batch["receipts"] = receipts
        batch["quota"] = [{"day": int(row["day"]), "amount": int(row["amount"])} for row in quota_rows]
        return batch

    def get_save_result(self, batch_no: str, save_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result FROM batch_saves WHERE batch_no=? AND save_id=?", (batch_no, save_id)
            ).fetchone()
        return json.loads(row["result"]) if row is not None else None

    def quota_status(self, day: int) -> Dict[str, int]:
        with self._connect() as connection:
            limits = connection.execute("SELECT DISTINCT quota_per_day FROM batches").fetchall()
            used_row = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS used FROM quota_allocations WHERE day=?", (day,)
            ).fetchone()
        per_day = max([int(row["quota_per_day"]) for row in limits], default=50)
        return {"day": day, "used": int(used_row["used"]), "per_day": per_day,
                "remaining": max(0, per_day - int(used_row["used"]))}

    # ------------------------------------------------------------------
    # 单案件接口（保留原语义，同步登记批次版本）
    # ------------------------------------------------------------------
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, batch_no: str = BATCH_NO_SYSTEM) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,batch_no,batch_version)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now, batch_no, 1),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1,
                     json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now, batch_no, 1),
                )
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

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            batch_no = row["batch_no"] or BATCH_NO_SYSTEM
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=?,batch_no=?,batch_version=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 actor_id, now, batch_no, version, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (record_id, action, actor_id, version,
                 json.dumps(details, ensure_ascii=False, sort_keys=True), now, batch_no, version),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]),
                 json.dumps(details, ensure_ascii=False, sort_keys=True), _now(),
                 row["batch_no"], row["batch_version"]),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

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

    # ------------------------------------------------------------------
    # 历史数据回填：缺批次号的旧案件先补原承办人和日期，补不全的停在待核
    # ------------------------------------------------------------------
    def backfill_legacy(self) -> Dict[str, Any]:
        now = _now()
        report = {"backfilled": 0, "pending_verification": 0, "batches": []}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM records WHERE batch_no IS NULL OR batch_version IS NULL ORDER BY id"
            ).fetchall()
            batches: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                item = dict(row)
                handler = item.get("created_by") or ""
                created_at = item.get("created_at") or ""
                payload = json.loads(item["payload"])
                if handler.strip() and created_at.strip():
                    batch_no = "%s%s" % (LEGACY_BATCH_PREFIX, handler)
                    if batch_no not in batches:
                        batches[batch_no] = {"batch_no": batch_no, "handled": 0}
                        connection.execute(
                            "INSERT INTO batches(batch_no,application_id,version,quota_per_day,snapshot,created_by,updated_by,created_at,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?)",
                            (batch_no, batch_no, 1, 50,
                             json.dumps({"legacy": True}, ensure_ascii=False),
                             handler, handler, created_at, created_at),
                        )
                    connection.execute(
                        "UPDATE records SET batch_no=?,batch_version=1 WHERE id=?", (batch_no, item["id"])
                    )
                    connection.execute(
                        "UPDATE audit_events SET batch_no=?,batch_version=COALESCE(batch_version,1) WHERE record_id=? AND batch_no IS NULL",
                        (batch_no, item["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (item["id"], "legacy_backfill", handler, int(item["version"]),
                         json.dumps({"batch_no": batch_no, "original_handler": handler,
                                     "original_date": created_at}, ensure_ascii=False, sort_keys=True),
                         now, batch_no, 1),
                    )
                    batches[batch_no]["handled"] += 1
                    report["backfilled"] += 1
                else:
                    connection.execute(
                        "UPDATE records SET state=?,batch_no=?,batch_version=?,updated_at=? WHERE id=?",
                        ("pending_verification", "LEGACY-UNVERIFIED", 0, now, item["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,batch_no,batch_version)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (item["id"], "legacy_backfill_pending", "system", int(item["version"]),
                         json.dumps({"reason": "缺少原承办人或受理日期，停在待核"}, ensure_ascii=False),
                         now, "LEGACY-UNVERIFIED", 0),
                    )
                    report["pending_verification"] += 1
            summary = list(batches.values())
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                (META_BACKFILL, json.dumps(summary, ensure_ascii=False, sort_keys=True)),
            )
            connection.commit()
        report["batches"] = summary
        return report

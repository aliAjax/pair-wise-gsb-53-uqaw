"""业务用例编排、权限检查与审计。"""
import json
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, text
from .repository import Repository, _now
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self._backfill_once()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 联合申请批次版本
    # ------------------------------------------------------------------
    def _backfill_once(self) -> None:
        # 回填幂等且已做容错；若极端情况下失败，可通过管理员接口重试，不阻断启动
        self.repository.backfill_legacy()

    def backfill(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role != "admin" and actor.role != "supervisor":
            raise PermissionDenied("仅管理员可执行历史回填")
        return self.repository.backfill_legacy()

    def create_batch(self, actor: Actor, batch_no: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        if not self.rules.role_can_batch(actor.role):
            raise PermissionDenied("角色无权创建批次")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        p = self.rules.validate_batch_create(payload or {})
        return self.repository.create_batch(batch_no, p["application_id"], int(p["quota_per_day"]), actor.user_id)

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_detail(batch_no)

    def list_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(limit=limit)

    def quota_status(self, actor: Actor, day: int) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.quota_status(int(day))

    def save_batch(self, actor: Actor, batch_no: str, expected_version: int, save_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """整批保存：案件、材料回执、办理名额、留痕共用同一批次版本。

        - 回执编号重复时只补差异，不新增名额；
        - 主案事实更新时，待办附属案失效重算，已决定案件保留原依据进入复核；
        - 两位经办同时保存只允许一个版本落盘，另一个收到409并拿到最新批次快照；
        - save_id 重试直接回放首次结果，不重复占名额或留痕。
        """
        actor = self._actor(actor)
        if not self.rules.role_can_batch(actor.role):
            raise PermissionDenied("角色无权保存批次")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        save_id = text({"save_id": save_id}, "save_id")
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        request = request or {}

        # 幂等回放优先：客户端按save_id重试时，携带的可能是保存前的旧基准版本
        prior = self.repository.get_save_result(batch_no, save_id)
        if prior is not None:
            prior["replayed"] = True
            return prior

        # 只读预校验（冲突的最终判定在写锁内完成）
        batch = self.repository.get_batch(batch_no)
        if int(batch["version"]) != int(expected_version):
            raise self._stale(batch)
        members_raw = request.get("members", [])
        if not isinstance(members_raw, list):
            raise ValidationError("members必须是列表")
        prepared_members = [self.rules.validate_member(m) for m in members_raw]
        receipts_raw = request.get("receipts", [])
        if not isinstance(receipts_raw, list):
            raise ValidationError("receipts必须是列表")
        prepared_receipts = [self.rules.validate_receipt(r) for r in receipts_raw]
        primary_incoming = next((m for m in prepared_members if m.get("applicant_role") == "primary"), None)

        with self.repository.transaction() as tx:
            locked = tx.get_batch_locked(batch_no)
            if locked is None:
                raise NotFound("批次不存在")
            current_version = int(locked["version"])

            # 幂等回放：同一save_id重试，返回首次落盘结果（锁内兜底）
            prior = tx.get_save(batch_no, save_id)
            if prior is not None:
                result = _loads(prior["result"])
                result["replayed"] = True
                return result

            # 乐观并发：版本已前进则拒绝，另一位经办的填写内容由调用方保留
            if current_version != int(expected_version):
                latest = self.repository.batch_detail(batch_no)
                raise Conflict(
                    "批次版本已前进（当前v%s，期望v%s），请合并后重试" % (current_version, expected_version),
                    extra={"current_version": current_version, "batch": latest, "code": "batch_version_conflict"},
                )

            existing_members = tx.list_members(batch_no)
            existing_by_ref = {m["reference"]: m for m in existing_members}
            existing_primary = next((m for m in existing_members if m["payload"].get("applicant_role") == "primary"), None)
            fact_change: Dict[str, Any] = {}
            fact_reason = ""
            if primary_incoming is not None and existing_primary is not None:
                fact_change = self.rules.primary_fact_changes(existing_primary["payload"], primary_incoming)
                if fact_change:
                    fact_reason = "主案事实更新：" + ", ".join(sorted(fact_change.keys()))

            new_version = current_version + 1
            saved_members: List[Dict[str, Any]] = []
            member_report: List[Dict[str, Any]] = []
            cascades: List[Dict[str, Any]] = []
            now = _now()

            # 1) 先落本次提交的成员（主案优先，保证级联依据最新）
            ordered = sorted(prepared_members, key=lambda m: 0 if m.get("applicant_role") == "primary" else 1)
            for incoming in ordered:
                reference = text(incoming, "reference")
                del incoming["reference"]
                if reference in existing_by_ref:
                    existing = existing_by_ref[reference]
                    payload, changed = self.rules.build_member_payload(existing["payload"], incoming)
                    member = tx.update_member(
                        existing["id"], existing["version"], existing["state"], payload,
                        actor.user_id, new_version, now,
                    )
                    action = "member_update" if changed else "member_noop"
                else:
                    incoming["basis_batch_version"] = new_version
                    member = tx.insert_member(
                        reference, self.rules.INITIAL_STATE, incoming,
                        actor.user_id, batch_no, new_version, now,
                    )
                    payload, changed, action = incoming, incoming, "member_create"
                saved_members.append(member)
                idem = "%s:%s:member:%s" % (batch_no, save_id, reference)
                tx.audit(member["id"], action, actor.user_id, member["version"], batch_no, new_version,
                         {"reference": reference, "changed": sorted(changed.keys())}, idem_key=idem, now=now)
                member_report.append({"reference": reference, "action": action, "id": member["id"],
                                      "version": member["version"], "changed": sorted(changed.keys())})

            # 2) 主案事实级联：受影响的待办案件马上失效重算，已决定案件进复核
            refreshed_by_id = {m["id"]: m for m in saved_members}
            if fact_change and existing_primary is not None:
                primary_now = refreshed_by_id.get(existing_primary["id"], existing_primary)
                for member in existing_members:
                    if member["id"] == primary_now["id"]:
                        continue
                    # 本次提交中被直接更新的成员，以其落盘后的payload为级联输入
                    member = refreshed_by_id.get(member["id"], member)
                    outcome = self.rules.apply_primary_fact_change(member, fact_change, current_version, fact_reason)
                    if outcome["action"] == "unchanged":
                        continue
                    cascades.append(outcome)
                    payload = dict(outcome["payload"])
                    payload["basis_batch_version"] = new_version
                    updated = tx.update_member(
                        member["id"], member["version"], outcome["state"], payload,
                        actor.user_id, new_version, now,
                    )
                    refreshed_by_id[member["id"]] = updated
                    idem = "%s:%s:cascade:%s" % (batch_no, save_id, member["reference"])
                    if outcome["action"] == "recompute":
                        tx.audit(member["id"], "fact_invalidated", actor.user_id, updated["version"] - 1,
                                 batch_no, new_version,
                                 {"reason": fact_reason, "stale_fields": outcome["stale_fields"],
                                  "basis_batch_version": current_version},
                                 idem_key=idem + ":invalidate", now=now)
                        tx.audit(member["id"], "fact_recalculated", actor.user_id, updated["version"],
                                 batch_no, new_version,
                                 {"reason": fact_reason, "stale_fields": outcome["stale_fields"],
                                  "basis_batch_version": new_version},
                                 idem_key=idem + ":recalc", now=now)
                    else:
                        tx.audit(member["id"], "enter_review", actor.user_id, updated["version"],
                                 batch_no, new_version,
                                 {"reason": fact_reason, "from_state": member["state"],
                                  "basis_snapshot": payload.get("basis_snapshot"),
                                  "basis_batch_version": current_version},
                                 idem_key=idem, now=now)

            # 3) 材料回执：编号重复只补差异，不再新增名额
            receipt_report: List[Dict[str, Any]] = []
            new_quota_days: Dict[int, int] = {}
            primary_id = None
            if existing_primary is not None:
                primary_id = refreshed_by_id.get(existing_primary["id"], existing_primary)["id"]
            else:
                for member in saved_members:
                    if member["payload"].get("applicant_role") == "primary":
                        primary_id = member["id"]
                        break
            if prepared_receipts and primary_id is None:
                raise ValidationError("回执必须挂在主案下，请先在批次中提交primary成员")
            for incoming in prepared_receipts:
                receipt_no = incoming["receipt_no"]
                existing_receipt = tx.get_receipt(receipt_no)
                idem = "%s:%s:receipt:%s" % (batch_no, save_id, receipt_no)
                if existing_receipt is None:
                    day = request_day(request, receipt_no)
                    receipt = tx.insert_receipt(receipt_no, incoming["documents"], incoming["note"],
                                                actor.user_id, batch_no, new_version, now)
                    new_quota_days[day] = new_quota_days.get(day, 0) + 1
                    action = "receipt_create"
                else:
                    merged_documents = _merge_documents(existing_receipt["documents"], incoming["documents"])
                    note = incoming["note"] or existing_receipt["note"]
                    diff = self.rules.receipt_diff(
                        {"documents": existing_receipt["documents"], "note": existing_receipt["note"]},
                        {"documents": merged_documents, "note": note},
                    )
                    if diff:
                        receipt = tx.update_receipt(existing_receipt["id"], merged_documents, note,
                                                    actor.user_id, batch_no, new_version, now)
                        action = "receipt_diff_append"
                    else:
                        receipt = existing_receipt
                        action = "receipt_duplicate_noop"
                tx.audit(primary_id, action, actor.user_id, new_version,
                         batch_no, new_version, {"receipt_no": receipt_no}, idem_key=idem, now=now)
                receipt_report.append({"receipt_no": receipt_no, "action": action})

            # 4) 当日受理名额：仅对新回执占用，先查全局用量再写入（UNIQUE兜底重试幂等）
            quota_report: List[Dict[str, Any]] = []
            for day, amount in sorted(new_quota_days.items()):
                used = tx.quota_used(day)
                if used + amount > int(locked["quota_per_day"]):
                    raise ValidationError(
                        "第%s天受理名额不足：已用%s/上限%s，本次需%s"
                        % (day, used, locked["quota_per_day"], amount)
                    )
                tx.allocate_quota(day, amount, batch_no, save_id, now)
                quota_report.append({"day": day, "allocated": amount, "used_after": used + amount,
                                     "per_day": int(locked["quota_per_day"])})

            # 5) 批次版本前进 + 保存结果（失败恢复与重试回放依据）
            final_members = tx.list_members(batch_no)
            final_receipts = tx.list_receipts(batch_no)
            cumulative_quota = tx.list_quota(batch_no)
            snapshot = {
                "batch_no": batch_no,
                "version": new_version,
                "application_id": locked["application_id"],
                "members": [_snapshot_member(m) for m in final_members],
                "receipts": [_snapshot_receipt(r) for r in final_receipts],
                "quota": cumulative_quota,
                "quota_per_day": int(locked["quota_per_day"]),
            }
            result = {
                "batch_no": batch_no,
                "version": new_version,
                "save_id": save_id,
                "members": member_report,
                "receipts": receipt_report,
                "quota": quota_report,
                "fact_change": sorted(fact_change.keys()),
                "cascades": [{"reference": next((m["reference"] for m in final_members if m["id"] == c["member_id"]), c["member_id"]),
                              "action": c["action"], **({"state": c["state"]} if "state" in c else {})}
                             for c in cascades],
                "snapshot": snapshot,
                "replayed": False,
            }
            tx.advance_batch(batch_no, new_version, actor.user_id, snapshot, save_id, now)
            tx.record_save(batch_no, save_id, current_version, new_version, actor.user_id,
                           _jsonable(request), result, now)
            return result

    @staticmethod
    def _stale(batch: Dict[str, Any]) -> Conflict:
        return Conflict(
            "批次版本已前进，请合并后重试",
            extra={"current_version": int(batch["version"]),
                   "batch": {k: v for k, v in batch.items()},
                   "code": "batch_version_conflict"},
        )


def _loads(raw: str) -> Dict[str, Any]:
    return json.loads(raw)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _merge_documents(old: List[str], new: List[str]) -> List[str]:
    merged = list(old)
    for doc in new:
        if doc not in merged:
            merged.append(doc)
    return merged


def request_day(request: Dict[str, Any], receipt_no: str) -> int:
    days = request.get("quota_days")
    if isinstance(days, dict) and receipt_no in days and isinstance(days[receipt_no], int):
        return int(days[receipt_no])
    day = request.get("quota_day")
    if isinstance(day, int):
        return int(day)
    raise ValidationError("新回执%s需要提供quota_day（或quota_days映射）" % receipt_no)


def _snapshot_member(member: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": member["id"], "reference": member["reference"], "state": member["state"],
            "version": member["version"], "payload": member["payload"]}


def _snapshot_receipt(receipt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if receipt is None:
        return {}
    return {"receipt_no": receipt["receipt_no"], "documents": receipt["documents"], "note": receipt["note"]}

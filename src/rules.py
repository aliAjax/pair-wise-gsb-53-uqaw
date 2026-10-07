"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}

# 联合申请批次相关状态
REVIEW_STATE = "review"                       # 主案事实变化后，已决定案件保留原依据进入复核
PENDING_VERIFICATION_STATE = "pending_verification"  # 历史数据补不全，停在待核
# 主案事实变化时需要立刻失效重算的待办状态
PENDING_STATES = {"draft", "submitted", "evidence_requested", "response_received"}
# 已决定、不受自动重算影响、转入复核的状态
DECIDED_STATES = {"decided", "appealed"}
# 冻结状态：归档与待核不参与级联
FROZEN_STATES = {"closed", PENDING_VERIFICATION_STATE, REVIEW_STATE}
# 主案事实字段：变化即触发附属案级联
PRIMARY_FACT_FIELDS = (
    "applicant_id", "case_type", "received_day", "deadline_days",
    "representation_active", "required_documents", "guarantor_id",
)
# 主案变化后需要同步到附属案的共享事实（申请人/案件类型仍以分申请人建档为准）
SHARED_FACT_FIELDS = (
    "received_day", "deadline_days", "representation_active",
    "required_documents", "guarantor_id",
)
BATCH_ROLES = {'intake_officer', 'case_officer', 'supervisor', 'legal_rep'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles or role in BATCH_ROLES

    def role_can_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_ROLES

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 联合申请批次
    # ------------------------------------------------------------------
    def validate_batch_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "application_id")
        p["quota_per_day"] = integer(p, "quota_per_day", 1) if "quota_per_day" in p else 50
        return p

    def validate_member(self, member: Dict[str, Any]) -> Dict[str, Any]:
        m = dict(member or {})
        m["role"] = choice(m, "role", ["primary", "dependent"])
        role = m.pop("role")
        prepared = self.prepare_create(m)
        prepared["applicant_role"] = role
        return prepared

    @staticmethod
    def diff_payload(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """只返回实际发生变化的字段。"""
        changed: Dict[str, Any] = {}
        for key, value in new.items():
            if old.get(key) != value:
                changed[key] = value
        return changed

    def primary_fact_changes(self, old_primary: Dict[str, Any], new_primary: Dict[str, Any]) -> Dict[str, Any]:
        changed = self.diff_payload(old_primary, new_primary)
        return {key: changed[key] for key in PRIMARY_FACT_FIELDS if key in changed}

    def build_member_payload(self, existing: Dict[str, Any], incoming: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """合并批次成员材料，返回新payload与差异字段。"""
        payload = dict(existing)
        changed: Dict[str, Any] = {}
        for key, value in incoming.items():
            if payload.get(key) != value:
                payload[key] = value
                changed[key] = value
        if changed:
            payload["missing_documents"] = [
                doc for doc in payload.get("required_documents", [])
                if doc not in payload.get("submitted_documents", [])
            ]
        return payload, changed

    def apply_primary_fact_change(self, member: Dict[str, Any], fact: Dict[str, Any], from_version: int, reason: str) -> Dict[str, Any]:
        """主案事实更新后对单个附属成员的级联结果。

        - 待办案件：原依据立刻失效并按新事实重算；
        - 已决定案件：保留原依据快照，进入复核；
        - 归档/待核/已复核案件：冻结，不做处理。
        """
        state = member["state"]
        payload = dict(member["payload"])
        result: Dict[str, Any] = {"member_id": member["id"], "action": "unchanged"}
        if state in FROZEN_STATES:
            return result
        if state in DECIDED_STATES:
            payload["basis_snapshot"] = {
                "state": state,
                "payload": dict(member["payload"]),
                "basis_batch_version": from_version,
            }
            payload["review_reason"] = reason
            result.update(action="enter_review", state=REVIEW_STATE, payload=payload)
            return result
        if state in PENDING_STATES:
            stale = {key: value for key, value in fact.items() if key in SHARED_FACT_FIELDS}
            # 原待办依据失效，随后按新事实重算
            invalidated = dict(payload)
            invalidated["invalidated_basis_batch_version"] = from_version
            invalidated["invalidated_reason"] = reason
            recalculated = self._recompute_facts(invalidated, stale)
            recalculated["basis_batch_version"] = None  # 由仓储层在落盘时写入新版本
            result.update(action="recompute", state=state, payload=recalculated, stale_fields=stale)
            return result
        result["action"] = "unchanged"
        return result

    @staticmethod
    def _recompute_facts(payload: Dict[str, Any], fact: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p.update(fact)
        received_day = int(p.get("received_day", 0))
        deadline_days = int(p.get("deadline_days", 1))
        p["deadline_day"] = received_day + deadline_days
        p["days_remaining"] = p["deadline_day"] - int(p.get("response_day", 0))
        p["overdue"] = p["days_remaining"] < 0
        required = list(p.get("required_documents", []))
        p["required_documents"] = required
        p["missing_documents"] = [
            doc for doc in required if doc not in p.get("submitted_documents", [])
        ]
        p.pop("invalidated_basis_batch_version", None)
        return p

    def validate_receipt(self, data: Dict[str, Any]) -> Dict[str, Any]:
        r = dict(data or {})
        text(r, "receipt_no")
        r["documents"] = text_list(r, "documents", 1)
        r["note"] = str(r.get("note", "") or "")
        return r

    @staticmethod
    def receipt_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in new.items() if old.get(key) != value}

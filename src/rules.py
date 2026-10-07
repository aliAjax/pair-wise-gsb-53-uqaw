"""移民案件期限与材料管理领域规则与状态转换。"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
PENDING_VERIFICATION = "pending_verification"
DECIDED_STATES = {"decided", "appealed", "closed"}
TODO_STATES = {"draft", "submitted", "evidence_requested", "response_received"}
CREATE_ROLES = {'intake_officer'}
BATCH_SAVE_ROLES = {'intake_officer', 'case_officer', 'supervisor'}
ACTION_ROLES = {
    'submit': {'legal_rep', 'case_officer'},
    'request_evidence': {'case_officer'},
    'respond': {'legal_rep'},
    'decide': {'case_officer', 'supervisor'},
    'appeal': {'legal_rep'},
    'close': {'supervisor'},
}
TRANSITIONS = {
    'submit': {'draft': 'submitted'},
    'request_evidence': {'submitted': 'evidence_requested'},
    'respond': {'evidence_requested': 'response_received'},
    'decide': {'submitted': 'decided', 'response_received': 'decided'},
    'appeal': {'decided': 'appealed'},
    'close': {'decided': 'closed', 'appealed': 'closed'},
}
FACT_KEYS = [
    "applicant_id",
    "case_type",
    "received_day",
    "deadline_days",
    "response_day",
    "representation_active",
    "sponsor_id",
    "sponsor_active",
    "sponsor_withdrawn",
    "applicant_withdrawn",
]


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(BATCH_SAVE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_save_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_SAVE_ROLES

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
        p.update(self._sponsor_defaults(p))
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["stale_documents"] = []
        p["document_versions"] = {}
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided", "pending_verification"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if record.get("state") == PENDING_VERIFICATION or record.get("review_status") == PENDING_VERIFICATION:
            raise Conflict("待核历史案件不能继续办理")
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

    def prepare_batch_plan(self, snapshot: Dict[str, Any], request: Dict[str, Any], actor_id: str, create: bool = False) -> Dict[str, Any]:
        if snapshot.get("status") == PENDING_VERIFICATION:
            raise Conflict("待核历史批次不能继续办理")
        expected_version = 0 if create else integer(request, "expected_version", 0)
        if not create and expected_version != int(snapshot.get("version", 0)):
            raise Conflict(
                "批次版本已前进，请保留填写内容后基于新版本重试",
                {"current_version": int(snapshot.get("version", 0)), "snapshot": snapshot},
            )
        save_token = text(request, "save_token")
        quota_date = self._quota_date(request.get("quota_date") if create else request.get("quota_date", snapshot.get("quota", {}).get("date")))
        quota_limit = None
        if "quota_limit" in request and request["quota_limit"] is not None:
            quota_limit = integer(request, "quota_limit", 0)
        elif not create:
            quota_limit = snapshot.get("quota", {}).get("limit")

        raw_cases = request.get("cases", [])
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValidationError("cases至少需要一个案件")
        cases = [self._normalize_case(item) for item in raw_cases]
        references = [case["reference"] for case in cases]
        if len(set(references)) != len(references):
            raise ValidationError("批次内案件引用不能重复")
        mains = [case for case in cases if case["is_main"]]
        if len(mains) != 1:
            raise ValidationError("每个批次必须且只能有一个主案")
        main_ref = mains[0]["reference"]
        current_cases = {item["reference"]: item for item in snapshot.get("cases", [])}
        if create and current_cases:
            raise Conflict("批次已存在")
        for case in cases:
            if case["reference"] in current_cases and current_cases[case["reference"]].get("state") == PENDING_VERIFICATION:
                raise Conflict("待核历史案件不能继续办理")
            if not case["is_main"] and case["main_reference"] != main_ref:
                raise ValidationError("附属案必须指向本批次主案：%s" % case["reference"])

        if not create and set(current_cases) - set(references):
            raise Conflict("完整批次保存不能遗漏既有案件", {"current_version": expected_version, "snapshot": snapshot})

        raw_receipts = request.get("receipts", [])
        if raw_receipts is None:
            raw_receipts = []
        if not isinstance(raw_receipts, list):
            raise ValidationError("receipts必须是列表")
        target_version = expected_version + 1
        receipts = [
            self._normalize_receipt(item, references, snapshot.get("receipts", []), target_version)
            for item in raw_receipts
        ]
        receipt_numbers = [receipt["receipt_number"] for receipt in receipts]
        if len(set(receipt_numbers)) != len(receipt_numbers):
            raise ValidationError("同一批次保存中的回执编号不能重复")
        doc_versions = self._document_versions(receipts)

        main_facts = next(case["facts"] for case in cases if case["is_main"])
        for case in cases:
            if not case["is_main"]:
                case["facts"]["sponsor_id"] = main_facts["sponsor_id"]
                case["facts"]["sponsor_active"] = main_facts["sponsor_active"]
                case["facts"]["sponsor_withdrawn"] = main_facts["sponsor_withdrawn"]
        current_main = current_cases.get(main_ref, {})
        main_changed = bool(current_main) and main_facts != self._facts_from_payload(current_main.get("payload", {}))
        case_plans: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []

        for case in cases:
            current = current_cases.get(case["reference"])
            exists = current is not None
            state = current["state"] if exists else INITIAL_STATE
            old_payload = dict(current.get("payload", {})) if exists else {}
            affected = main_changed and (case["is_main"] or not case["is_main"])
            decided = state in DECIDED_STATES
            basis_version = int(current.get("basis_version") or 1) if exists else 1
            review_status = current.get("review_status", "") if exists else ""

            if exists and decided and affected:
                payload = old_payload
                basis_version = int(current.get("basis_version") or 1)
                review_status = "review_pending"
                events.append({
                    "action": "review_required",
                    "case_reference": case["reference"],
                    "event_key": "case:review_required:%s:%s" % (case["reference"], target_version),
                    "details": {
                        "reason": "主案事实已更新，已决定案件保留原依据并进入复核",
                        "basis_version": basis_version,
                    },
                })
            else:
                if exists and state in TODO_STATES:
                    payload = old_payload
                    payload.update(case["facts"])
                    required = case.get("required_documents") or old_payload.get("required_documents") or ["passport"]
                    reset_basis = affected
                    if case["is_main"] and main_changed:
                        basis_version = target_version
                        review_status = ""
                        events.append({
                            "action": "fact_updated",
                            "case_reference": case["reference"],
                            "event_key": "case:fact_updated:%s:%s" % (case["reference"], target_version),
                            "details": {"facts": case["facts"], "from_version": expected_version},
                        })
                    elif affected:
                        basis_version = target_version
                        review_status = ""
                    payload = self._recompute_payload(
                        payload,
                        required,
                        case,
                        main_facts,
                        doc_versions.get(case["reference"], {}),
                        basis_version,
                        reset_basis,
                    )
                    if affected:
                        events.append({
                            "action": "todos_invalidated",
                            "case_reference": case["reference"],
                            "event_key": "case:todos_invalidated:%s:%s" % (case["reference"], target_version),
                            "details": {
                                "reason": "主案事实更新，待办按新批次版本失效重算",
                                "basis_version": basis_version,
                                "missing_documents": payload["missing_documents"],
                                "stale_documents": payload["stale_documents"],
                            },
                        })
                elif exists:
                    payload = old_payload
                else:
                    payload = self._new_payload(case, main_facts, doc_versions.get(case["reference"], {}))
                    basis_version = 1
                    events.append({
                        "action": "case_created",
                        "case_reference": case["reference"],
                        "event_key": "case:created:%s" % case["reference"],
                        "details": {"facts": case["facts"]},
                    })

            case_plans.append({
                "reference": case["reference"],
                "exists": exists,
                "state": state,
                "payload": payload,
                "basis_version": basis_version,
                "review_status": review_status,
                "main_reference": None if case["is_main"] else main_ref,
            })

        existing_receipt_numbers = {item["receipt_number"] for item in snapshot.get("receipts", [])}
        for receipt in receipts:
            if receipt["receipt_number"] in existing_receipt_numbers:
                if receipt.get("patch"):
                    events.append({
                        "action": "receipt_patched",
                        "event_key": "receipt:patched:%s:%s" % (receipt["receipt_number"], target_version),
                        "details": {
                            "receipt_number": receipt["receipt_number"],
                            "added_documents": receipt["patch"]["added_documents"],
                            "changed_fields": receipt["patch"]["changed_fields"],
                        },
                    })
            else:
                events.append({
                    "action": "receipt_created",
                    "event_key": "receipt:created:%s" % receipt["receipt_number"],
                    "details": {"receipt_number": receipt["receipt_number"], "case_references": receipt["case_references"]},
                })
                events.append({
                    "action": "quota_reserved",
                    "event_key": "quota:reserved:%s" % receipt["receipt_number"],
                    "details": {"quota_date": quota_date, "receipt_number": receipt["receipt_number"]},
                })

        notes = request.get("notes", [])
        if notes is None:
            notes = []
        if not isinstance(notes, list):
            raise ValidationError("notes必须是列表")
        used_keys = {event["event_key"] for event in events}
        for index, note in enumerate(notes):
            if not isinstance(note, dict):
                raise ValidationError("notes项必须是对象")
            event_key = text(note, "event_key")
            if event_key in used_keys:
                raise ValidationError("留痕事件键重复：%s" % event_key)
            used_keys.add(event_key)
            case_ref = note.get("case_reference", "")
            if case_ref and case_ref not in references:
                raise ValidationError("留痕引用了不存在的案件")
            events.append({
                "action": text(note, "text") and "note",
                "case_reference": case_ref or None,
                "event_key": event_key,
                "details": {"text": text(note, "text"), "index": index},
            })

        return {
            "save_token": save_token,
            "quota_date": quota_date,
            "quota_limit": quota_limit,
            "cases": case_plans,
            "receipts": [{k: v for k, v in receipt.items() if k != "patch"} for receipt in receipts],
            "events": events,
            "actor_id": actor_id,
            "expected_version": expected_version,
        }

    def _normalize_case(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("cases项必须是对象")
        reference = text(raw, "reference")
        data = raw.get("data", raw.get("facts", {}))
        if not isinstance(data, dict):
            raise ValidationError("案件data必须是对象")
        facts = self._sponsor_defaults({
            "applicant_id": text(data, "applicant_id"),
            "case_type": choice(data, "case_type", ["asylum", "family", "work"]),
            "received_day": integer(data, "received_day", 0),
            "deadline_days": integer(data, "deadline_days", 1),
            "response_day": integer(data, "response_day", 0),
            "representation_active": boolean(data, "representation_active"),
            "sponsor_id": self._optional_sponsor_text(data, "sponsor_id"),
            "sponsor_active": boolean(data, "sponsor_active", True),
            "sponsor_withdrawn": boolean(data, "sponsor_withdrawn", False),
            "applicant_withdrawn": boolean(data, "applicant_withdrawn", False),
        })
        required_documents = None
        if "required_documents" in data:
            required_documents = text_list(data, "required_documents", 1)
        return {
            "reference": reference,
            "facts": facts,
            "required_documents": required_documents,
            "is_main": boolean(raw, "is_main", False),
            "main_reference": raw.get("main_reference", "").strip() if isinstance(raw.get("main_reference", ""), str) else "",
        }

    @staticmethod
    def _optional_sponsor_text(data: Dict[str, Any], key: str) -> str:
        value = data.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValidationError("%s必须是文本" % key)
        return value.strip()

    @staticmethod
    def _sponsor_defaults(payload: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(payload)
        result.setdefault("sponsor_id", "")
        result.setdefault("sponsor_active", True)
        result.setdefault("sponsor_withdrawn", False)
        result.setdefault("applicant_withdrawn", False)
        return result

    def _facts_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values = {key: payload.get(key) for key in FACT_KEYS}
        return self._sponsor_defaults(values)

    def _normalize_receipt(self, raw: Any, references: List[str], existing: List[Dict[str, Any]], target_version: int) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("receipts项必须是对象")
        number = text(raw, "receipt_number")
        issued_day = integer(raw, "issued_day", 0)
        documents = text_list(raw, "documents", 0) if "documents" in raw else []
        fields = raw.get("fields", {})
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            raise ValidationError("回执fields必须是对象")
        case_refs = raw.get("case_references", references)
        case_refs = text_list({"case_references": case_refs}, "case_references", 1)
        unknown = set(case_refs) - set(references)
        if unknown:
            raise ValidationError("回执引用了不存在的案件：" + ", ".join(sorted(unknown)))

        current = next((item for item in existing if item["receipt_number"] == number), None)
        if current is None:
            payload = {
                "documents": documents,
                "document_versions": {doc: target_version for doc in documents},
                "fields": fields,
            }
            patch = {"added_documents": documents, "changed_fields": sorted(fields.keys())}
        else:
            old_payload = dict(current.get("payload", {}))
            old_docs = list(old_payload.get("documents", []))
            old_versions = dict(old_payload.get("document_versions", {}))
            added = [doc for doc in documents if doc not in old_docs]
            merged_docs = old_docs + added
            for doc in added:
                old_versions[doc] = target_version
            old_fields = dict(old_payload.get("fields", {}))
            merged_fields = dict(old_fields)
            changed_fields = []
            for key, value in fields.items():
                if old_fields.get(key) != value:
                    changed_fields.append(key)
                merged_fields[key] = value
            if int(current.get("issued_day", issued_day)) != issued_day:
                changed_fields.append("issued_day")
            payload = {"documents": merged_docs, "document_versions": old_versions, "fields": merged_fields}
            case_refs = sorted(set(list(current.get("case_references", [])) + case_refs))
            patch = {"added_documents": added, "changed_fields": sorted(set(changed_fields))}
        return {
            "receipt_number": number,
            "issued_day": issued_day,
            "payload": payload,
            "case_references": case_refs,
            "patch": patch if patch["added_documents"] or patch["changed_fields"] else None,
        }

    @staticmethod
    def _document_versions(receipts: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
        result: Dict[str, Dict[str, int]] = {}
        for receipt in receipts:
            for ref in receipt["case_references"]:
                target = result.setdefault(ref, {})
                for doc, version in receipt["payload"].get("document_versions", {}).items():
                    target[doc] = min(int(target.get(doc, version)), int(version))
        return result

    def _new_payload(self, case: Dict[str, Any], main_facts: Dict[str, Any], doc_versions: Dict[str, int]) -> Dict[str, Any]:
        required = list(case.get("required_documents") or ["passport"])
        required = self._with_sponsor_document(required, case, main_facts)
        facts = dict(case["facts"])
        payload = dict(facts)
        payload["deadline_day"] = facts["received_day"] + facts["deadline_days"]
        payload["days_remaining"] = payload["deadline_day"] - facts["response_day"]
        payload["overdue"] = payload["days_remaining"] < 0
        payload["required_documents"] = required
        payload["document_versions"] = {}
        return self._recompute_payload(payload, required, case, main_facts, doc_versions, 1, False)

    def _recompute_payload(
        self,
        payload: Dict[str, Any],
        required_base: List[str],
        case: Dict[str, Any],
        main_facts: Dict[str, Any],
        doc_versions: Dict[str, int],
        basis_version: int,
        reset_basis: bool,
    ) -> Dict[str, Any]:
        required = self._with_sponsor_document(list(required_base), case, main_facts)
        payload["required_documents"] = required
        submitted = []
        stale = []
        for doc, version in sorted(doc_versions.items()):
            if doc == "sponsor_letter" and reset_basis and int(version) < int(basis_version):
                stale.append(doc)
            if doc not in required:
                continue
            if doc == "sponsor_letter" and reset_basis and int(version) < int(basis_version):
                continue
            submitted.append(doc)
        payload["submitted_documents"] = submitted
        payload["missing_documents"] = [doc for doc in required if doc not in submitted]
        payload["stale_documents"] = sorted(set(list(payload.get("stale_documents", [])) + stale))
        payload["document_versions"] = dict(doc_versions)
        payload["deadline_day"] = int(payload["received_day"]) + int(payload["deadline_days"])
        payload["days_remaining"] = int(payload["deadline_day"]) - int(payload["response_day"])
        payload["overdue"] = payload["days_remaining"] < 0
        return payload

    @staticmethod
    def _with_sponsor_document(required: List[str], case: Dict[str, Any], main_facts: Dict[str, Any]) -> List[str]:
        facts = main_facts if not case["is_main"] else case["facts"]
        active_sponsor = bool(
            facts.get("sponsor_id")
            and facts.get("sponsor_active", True)
            and not facts.get("sponsor_withdrawn", False)
            and not facts.get("applicant_withdrawn", False)
        )
        if active_sponsor and "sponsor_letter" not in required:
            required.append("sponsor_letter")
        if not active_sponsor:
            required = [doc for doc in required if doc != "sponsor_letter"]
        return list(dict.fromkeys(required))

    @staticmethod
    def _quota_date(value: Any) -> str:
        if value is None:
            from datetime import datetime, timezone
            return datetime.now(timezone.utc).date().isoformat()
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            raise ValidationError("quota_date必须是YYYY-MM-DD")
        return value.strip()

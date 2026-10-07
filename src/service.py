"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

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

    def create_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_save_batch(actor.role):
            raise PermissionDenied("角色无权保存批次")
        save_token = text(payload or {}, "save_token")
        committed = self.repository.get_committed_save_token(save_token)
        if committed is not None:
            return committed
        plan = self.rules.prepare_batch_plan({}, payload or {}, actor.user_id, create=True)
        return self.repository.create_batch(plan)

    def save_batch(self, actor: Actor, batch_no: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_save_batch(actor.role):
            raise PermissionDenied("角色无权保存批次")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        save_token = text(payload or {}, "save_token")
        committed = self.repository.get_committed_save(batch_no, save_token)
        if committed is not None:
            committed["batch_no"] = batch_no
            return committed
        snapshot = self.repository.get_batch_by_no(batch_no)
        plan = self.rules.prepare_batch_plan(snapshot, payload or {}, actor.user_id, create=False)
        result = self.repository.save_batch(batch_no, plan)
        result["batch_no"] = batch_no
        return result

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch_by_no(batch_no)

    def batch_timeline(self, actor: Actor, batch_no: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_timeline(batch_no)

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

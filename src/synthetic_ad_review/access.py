"""访问控制：队列分权、利益回避、证据分级可见。

- 平台运营者只能处理分配给自己的队列；
- 商品方（广告主及关联主体）不能审核/批准自己的广告；
- 法务可查看证据原件，普通审核员只能看到脱敏摘要。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Case, Evidence, ReviewerRole


class AccessError(PermissionError):
    """所有访问拒绝统一抛出，消息可直接回显给调用方。"""


@dataclass(frozen=True)
class User:
    username: str
    role: ReviewerRole
    queues: tuple[str, ...] = ()          # 被分配的队列
    affiliated_party_ids: tuple[str, ...] = ()  # 任职/代理/利益关联的主体


def require_queue(user: User, queue: str) -> None:
    """平台运营者/审核员只能操作分配给自己队列里的案件。"""
    if user.role is ReviewerRole.SYSTEM:
        return
    if queue not in user.queues:
        raise AccessError(f"{user.username} 未被分配到队列 {queue}，无权处理")


def require_not_interested_party(user: User, case: Case) -> None:
    """利益回避：广告主、获利方及其关联主体不能批准自己的广告。"""
    if user.role is ReviewerRole.SYSTEM:
        return
    interested = {case.advertiser_party_id}
    if case.beneficiary_party_id:
        interested.add(case.beneficiary_party_id)
    if case.product.link_owner_party_id:
        interested.add(case.product.link_owner_party_id)
    if case.product.registrant_party_id:
        interested.add(case.product.registrant_party_id)
    if set(user.affiliated_party_ids) & interested:
        raise AccessError(
            f"{user.username} 与该广告的责任主体存在利益关联，按回避原则不得审核本案"
        )


def require_review_access(user: User, case: Case) -> None:
    if user.role not in (ReviewerRole.MODERATOR, ReviewerRole.OPERATOR, ReviewerRole.LEGAL):
        raise AccessError(f"{user.role.label}无审核权限")
    require_queue(user, case.queue)
    require_not_interested_party(user, case)


def view_evidence(user: User, evidence: Evidence) -> dict[str, str]:
    """按岗位返回证据：法务见原件；其他岗位只见摘要。"""
    base = {
        "evidence_id": evidence.evidence_id,
        "kind": evidence.kind,
        "filename": evidence.filename,
        "sha256": evidence.sha256,
        "summary": evidence.summary,
    }
    if user.role is ReviewerRole.LEGAL:
        base["original_uri"] = evidence.original_uri
        base["note"] = "法务可调取原件"
        return base
    base["original_uri"] = "*** 仅法务可调取 ***"
    base["note"] = "普通审核员仅可见脱敏摘要"
    return base

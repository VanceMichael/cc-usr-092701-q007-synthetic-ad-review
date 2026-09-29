"""广告审查核心服务。

职责：
- 登记素材来源、身份授权、商品、脚本版本、投放与获利主体
- 提交幂等：重复提交沿用原结论
- 主体/商品冲突：挂起调查
- 按品类规则审核，脚本修改产生新版本并强制重审
- 权限：队列隔离、商品方不得批准自己的广告、证据分级可见
- 投放后撤销生成下架/通知/整改任务，逾期自动升级
- 申诉处理与全链路还原
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Optional

from . import models
from .models import (
    AILabelStatus,
    Appeal,
    AppealStatus,
    Asset,
    AuthorizationStatus,
    Case,
    CaseStatus,
    DecisionType,
    EnforcementTask,
    Org,
    PersonaIdentity,
    Placement,
    PlacementStatus,
    Product,
    ReviewDecision,
    ScriptVersion,
    Severity,
    TaskStatus,
    TaskType,
    User,
    UserRole,
)
from .rules import RULE_PACK_VERSION, evaluate
from .store import COLLECTIONS, Store

TAKEDOWN_WINDOW = timedelta(hours=24)
NOTIFY_WINDOW = timedelta(hours=24)
RECTIFY_WINDOW = timedelta(hours=72)
ESCALATION_GRACE = timedelta(hours=24)
ESCALATION_QUEUE = "escalation"


class DomainError(Exception):
    """业务规则冲突（状态不允许等）。"""


class AccessDenied(Exception):
    """权限不足或越权访问。"""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReviewService:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], datetime] = datetime.now,
        *,
        autosave: bool = True,
    ) -> None:
        self.store = store
        self.clock = clock
        self.autosave = autosave
        data = store.load()
        self.orgs: dict[str, Org] = {o.org_id: o for o in data["orgs"]}
        self.users: dict[str, User] = {u.user_id: u for u in data["users"]}
        self.identities: dict[str, PersonaIdentity] = {i.identity_id: i for i in data["identities"]}
        self.assets: dict[str, Asset] = {a.asset_id: a for a in data["assets"]}
        self.products: dict[str, Product] = {p.product_id: p for p in data["products"]}
        self.scripts: dict[str, ScriptVersion] = {s.version_id: s for s in data["scripts"]}
        self.cases: dict[str, Case] = {c.case_id: c for c in data["cases"]}
        self.decisions: dict[str, ReviewDecision] = {d.decision_id: d for d in data["decisions"]}
        self.placements: dict[str, Placement] = {p.placement_id: p for p in data["placements"]}
        self.tasks: dict[str, EnforcementTask] = {t.task_id: t for t in data["tasks"]}
        self.appeals: dict[str, Appeal] = {a.appeal_id: a for a in data["appeals"]}
        self._counters: dict[str, int] = {}
        self._restore_counters()
        # 恢复后立即推进时限：停机期间到期的任务会被扫描出来
        self.refresh_deadlines()

    def _restore_counters(self) -> None:
        """从已有编号恢复计数器，避免重载后生成重复 ID。"""
        indices = (
            self.orgs, self.users, self.identities, self.assets, self.products,
            self.scripts, self.cases, self.decisions, self.placements,
            self.tasks, self.appeals,
        )
        for index in indices:
            for key in index:
                if isinstance(key, str) and "-" in key:
                    prefix, _, num = key.rpartition("-")
                    if num.isdigit():
                        self._counters[prefix] = max(self._counters.get(prefix, 0), int(num))

    # ------------------------------------------------------------ 持久化

    def _snapshot(self) -> dict[str, list]:
        return {
            "orgs": list(self.orgs.values()),
            "users": list(self.users.values()),
            "identities": list(self.identities.values()),
            "assets": list(self.assets.values()),
            "products": list(self.products.values()),
            "scripts": list(self.scripts.values()),
            "cases": list(self.cases.values()),
            "decisions": list(self.decisions.values()),
            "placements": list(self.placements.values()),
            "tasks": list(self.tasks.values()),
            "appeals": list(self.appeals.values()),
        }

    def save(self) -> None:
        self.store.save(self._snapshot())

    def _touch(self) -> None:
        if self.autosave:
            self.save()

    def _next_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n:04d}"

    # ------------------------------------------------------------ 登记

    def register_org(self, org: Org) -> Org:
        if org.org_id in self.orgs:
            raise DomainError(f"主体已登记：{org.org_id}")
        self.orgs[org.org_id] = org
        self._touch()
        return org

    def register_user(self, user: User) -> User:
        if user.user_id in self.users:
            raise DomainError(f"用户已登记：{user.user_id}")
        if user.org_id not in self.orgs:
            raise DomainError("用户所属主体不存在")
        self.users[user.user_id] = user
        self._touch()
        return user

    def register_identity(self, identity: PersonaIdentity) -> PersonaIdentity:
        self.identities[identity.identity_id] = identity
        self._touch()
        return identity

    def register_asset(self, asset: Asset) -> Asset:
        self.assets[asset.asset_id] = asset
        self._touch()
        return asset

    def register_product(self, product: Product) -> Product:
        self.products[product.product_id] = product
        self._touch()
        return product

    # ------------------------------------------------------------ 提交

    @staticmethod
    def submission_key(
        asset_id: str, script_hash: str, product_id: str, channel: str, account_org_id: str
    ) -> str:
        raw = "|".join([asset_id, script_hash, product_id, channel, account_org_id])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def submit_case(
        self,
        *,
        title: str,
        asset_id: str,
        product_id: str,
        merchant_org_id: str,
        beneficiary_org_id: str,
        script_content: str,
        channel: str,
        account_org_id: str,
        account_name: str,
        author_org_id: Optional[str] = None,
    ) -> Case:
        asset = self._require(self.assets, asset_id, "素材")
        product = self._require(self.products, product_id, "商品")
        self._require(self.orgs, merchant_org_id, "广告主主体")
        self._require(self.orgs, beneficiary_org_id, "获利主体")
        self._require(self.orgs, account_org_id, "投放账户主体")
        author_org_id = author_org_id or merchant_org_id
        self._require(self.orgs, author_org_id, "脚本撰写主体")
        now = self.clock()

        script_hash = content_hash(script_content)
        key = self.submission_key(asset_id, script_hash, product_id, channel, account_org_id)

        # 幂等：完全相同的提交沿用原案件与原结论，不再走一遍审核
        existing = next((c for c in self.cases.values() if c.submission_key == key), None)
        if existing is not None:
            return existing

        case = Case(
            case_id=self._next_id("CASE"),
            title=title,
            asset_id=asset_id,
            product_id=product_id,
            merchant_org_id=merchant_org_id,
            beneficiary_org_id=beneficiary_org_id,
            submission_key=key,
            status=CaseStatus.PENDING_REVIEW,
            created_at=now,
        )

        script = ScriptVersion(
            version_id=self._next_id("SCR"),
            case_id=case.case_id,
            content=script_content,
            content_hash=script_hash,
            author_org_id=author_org_id,
            created_at=now,
        )
        self.scripts[script.version_id] = script
        case.current_script_version_id = script.version_id

        self._detect_conflicts(case, asset, product, account_org_id)
        self.cases[case.case_id] = case
        self._touch()
        return case

    def _detect_conflicts(self, case: Case, asset: Asset, product: Product, account_org_id: str) -> None:
        # 商品冲突：商品链接由非备案主体控制
        if product.link_org_id != product.registrant_org_id:
            case.conflict_notes.append(
                f"商品链接控制方 {product.link_org_id} 与备案主体 {product.registrant_org_id} 不一致"
            )
        # 主体冲突：实际获利方与广告主、备案主体均不同，属于隐蔽第三方获利
        if case.beneficiary_org_id not in (case.merchant_org_id, product.registrant_org_id):
            case.conflict_notes.append(
                f"实际获利方 {case.beneficiary_org_id} 与广告主、备案主体均不一致"
            )
        # 同一素材换主体/账户再次发布：阻止换号重发，挂起调查
        twin = next(
            (
                c for c in self.cases.values()
                if c.asset_id == case.asset_id
                and c.case_id != case.case_id
                and (c.merchant_org_id != case.merchant_org_id or c.submission_key != case.submission_key)
            ),
            None,
        )
        if twin is not None:
            case.conflict_notes.append(
                f"同一素材曾由其他主体/账户提交（关联案件 {twin.case_id}），涉嫌换号发布"
            )
        if case.conflict_notes:
            case.status = CaseStatus.UNDER_INVESTIGATION

    def resolve_conflict(self, case_id: str, reviewer: User, proceed: bool, note: str) -> Case:
        """调查结束：解除挂起进入审核，或直接驳回。"""
        case = self._require(self.cases, case_id, "案件")
        self._require_reviewer(reviewer, case)
        if case.status is not CaseStatus.UNDER_INVESTIGATION:
            raise DomainError("案件不在挂起调查状态")
        case.conflict_notes.append(f"调查结论（{reviewer.user_id}）：{note}")
        case.status = CaseStatus.PENDING_REVIEW if proceed else CaseStatus.REJECTED
        self._touch()
        return case

    # ------------------------------------------------------------ 脚本版本

    def revise_script(self, case_id: str, new_content: str, author_org_id: str) -> ScriptVersion:
        case = self._require(self.cases, case_id, "案件")
        if case.status is CaseStatus.UNDER_INVESTIGATION:
            raise DomainError("案件挂起调查期间不得修改脚本")
        if case.status in (CaseStatus.CLOSED, CaseStatus.REVOKED):
            raise DomainError("已撤销/结案的案件不能直接修改，请重新提交")
        new_hash = content_hash(new_content)
        current = self.scripts[case.current_script_version_id]
        if new_hash == current.content_hash:
            return current  # 内容未变，沿用当前版本
        now = self.clock()
        version = ScriptVersion(
            version_id=self._next_id("SCR"),
            case_id=case.case_id,
            content=new_content,
            content_hash=new_hash,
            author_org_id=author_org_id,
            created_at=now,
            supersedes=current.version_id,
        )
        self.scripts[version.version_id] = version
        case.current_script_version_id = version.version_id
        case.status = CaseStatus.PENDING_REVIEW  # 修改后必须重新审核
        self._touch()
        return version

    # ------------------------------------------------------------ 审核

    def _authorization_status(
        self, persona: PersonaIdentity | None, asset: Asset, now: datetime
    ) -> AuthorizationStatus | None:
        if persona is None:
            return None
        auth = next(
            (a for a in persona.authorizations if a.licensee_org_id == asset.producer_org_id),
            None,
        )
        if auth is None:
            return AuthorizationStatus.MISSING
        if not (auth.valid_from <= now <= auth.valid_to):
            return AuthorizationStatus.EXPIRED
        if "*" not in auth.scope and asset.asset_type.value not in auth.scope:
            return AuthorizationStatus.SCOPE_MISMATCH
        return AuthorizationStatus.VALID

    def evaluate_current(self, case: Case):
        script = self.scripts[case.current_script_version_id]
        product = self.products[case.product_id]
        asset = self.assets[case.asset_id]
        persona = self.identities.get(asset.persona_identity_id) if asset.persona_identity_id else None
        auth_status = self._authorization_status(persona, asset, self.clock())
        return evaluate(script, product, asset, persona, auth_status)

    def review_case(
        self, case_id: str, reviewer: User, decision: DecisionType, comments: str
    ) -> ReviewDecision:
        case = self._require(self.cases, case_id, "案件")
        self._require_reviewer(reviewer, case)
        if case.status is not CaseStatus.PENDING_REVIEW:
            raise DomainError(f"案件当前状态 {case.status.value} 不可审核")
        if decision is DecisionType.SUSPEND:
            raise DomainError("挂起由冲突调查流程产生，不能作为人工审核结论")

        evaluation = self.evaluate_current(case)
        # 商品方及任何责任关联方都不能裁决自己的广告（通过或驳回均需回避）
        responsible = self._responsible_orgs(case)
        if reviewer.org_id in responsible:
            raise AccessDenied("审核员属于广告责任关联方，不能裁决该广告")
        if decision is DecisionType.APPROVE and evaluation.blocked:
            raise DomainError("存在阻断级风险命中，不能通过；应驳回或补充材料")

        record = ReviewDecision(
            decision_id=self._next_id("REV"),
            case_id=case.case_id,
            script_version_id=case.current_script_version_id,
            reviewer_user_id=reviewer.user_id,
            rule_pack_version=RULE_PACK_VERSION,
            decision=decision,
            hits=evaluation.hits,
            comments=comments,
            created_at=self.clock(),
        )
        self.decisions[record.decision_id] = record

        if decision is DecisionType.APPROVE:
            case.status = CaseStatus.APPROVED
        elif self._active_placements(case):
            # 修改重审未过：已投放版本必须下架并通知、限期整改
            case.status = CaseStatus.REVOKED
            self._spawn_enforcement(case, comments)
        else:
            case.status = CaseStatus.REJECTED
        self._touch()
        return record

    def latest_decision(self, case_id: str) -> ReviewDecision | None:
        case_decisions = [d for d in self.decisions.values() if d.case_id == case_id]
        return case_decisions[-1] if case_decisions else None

    def _require_reviewer(self, user: User, case: Case) -> None:
        if UserRole.REVIEWER not in user.roles:
            raise AccessDenied("需要审核员角色")

    def _responsible_orgs(self, case: Case) -> set[str]:
        asset = self.assets[case.asset_id]
        product = self.products[case.product_id]
        script = self.scripts[case.current_script_version_id]
        return {
            case.merchant_org_id,
            case.beneficiary_org_id,
            asset.producer_org_id,
            product.registrant_org_id,
            product.link_org_id,
            script.author_org_id,
        }

    # ------------------------------------------------------------ 投放

    def place(
        self,
        case_id: str,
        *,
        channel: str,
        account_org_id: str,
        account_name: str,
    ) -> Placement:
        case = self._require(self.cases, case_id, "案件")
        if case.status is not CaseStatus.APPROVED:
            raise DomainError("只有审核通过的案件才能投放")
        self._require(self.orgs, account_org_id, "投放账户主体")
        placement = Placement(
            placement_id=self._next_id("PLM"),
            case_id=case_id,
            channel=channel,
            account_org_id=account_org_id,
            account_name=account_name,
            script_version_id=case.current_script_version_id,
            status=PlacementStatus.ACTIVE,
            placed_at=self.clock(),
        )
        self.placements[placement.placement_id] = placement
        case.placement_ids.append(placement.placement_id)
        self._touch()
        return placement

    def revoke(self, case_id: str, actor: User, reason: str) -> Case:
        """投放后撤销：生成下架、通知、限期整改任务。"""
        case = self._require(self.cases, case_id, "案件")
        if UserRole.REVIEWER not in actor.roles:
            raise AccessDenied("只有审核员可以发起撤销")
        if not self._active_placements(case):
            raise DomainError("案件没有在投放置，无需撤销")
        case.status = CaseStatus.REVOKED
        for placement in self._placements(case):
            if placement.status is PlacementStatus.ACTIVE:
                placement.status = PlacementStatus.SUSPENDED
        self._spawn_enforcement(case, reason)
        self._touch()
        return case

    def _spawn_enforcement(self, case: Case, reason: str) -> None:
        now = self.clock()
        for placement in self._placements(case):
            queue = f"ops:{placement.channel}"
            specs = (
                (TaskType.TAKEDOWN, now + TAKEDOWN_WINDOW),
                (TaskType.NOTIFY, now + NOTIFY_WINDOW),
                (TaskType.RECTIFY, now + RECTIFY_WINDOW),
            )
            for task_type, deadline in specs:
                # 同一投放、同一类型的未完成任务不重复生成
                exists = any(
                    t.case_id == case.case_id
                    and t.task_type is task_type
                    and t.status in (TaskStatus.PENDING, TaskStatus.OVERDUE, TaskStatus.ESCALATED)
                    and t.assignee_queue == queue
                    for t in self.tasks.values()
                )
                if exists:
                    continue
                task = EnforcementTask(
                    task_id=self._next_id("TSK"),
                    case_id=case.case_id,
                    task_type=task_type,
                    assignee_queue=queue,
                    created_at=now,
                    deadline=deadline,
                    result_note=reason,
                )
                self.tasks[task.task_id] = task

    # ------------------------------------------------------------ 队列与时限

    def queue_tasks(self, user: User) -> list[EnforcementTask]:
        if UserRole.OPERATOR not in user.roles:
            raise AccessDenied("需要平台运营者角色")
        return [
            t for t in self.tasks.values()
            if t.assignee_queue in user.queues and t.status is not TaskStatus.DONE
            and t.status is not TaskStatus.CANCELED
        ]

    def complete_task(self, task_id: str, user: User, note: str) -> EnforcementTask:
        task = self._require(self.tasks, task_id, "处置任务")
        if UserRole.OPERATOR not in user.roles:
            raise AccessDenied("需要平台运营者角色")
        if task.assignee_queue not in user.queues:
            raise AccessDenied("该任务不属于你的队列")
        if task.status in (TaskStatus.DONE, TaskStatus.CANCELED):
            raise DomainError("任务已结束")
        task.status = TaskStatus.DONE
        task.assignee_user_id = user.user_id
        task.result_note = note
        task.completed_at = self.clock()

        if task.task_type is TaskType.TAKEDOWN:
            for placement in self._placements(self.cases[task.case_id]):
                if placement.status in (PlacementStatus.ACTIVE, PlacementStatus.SUSPENDED):
                    placement.status = PlacementStatus.REMOVED
                    placement.removed_at = task.completed_at

        self._maybe_close(self.cases[task.case_id])
        self._touch()
        return task

    def refresh_deadlines(self) -> list[EnforcementTask]:
        """推进整改时限。服务恢复时调用：停机时间计入时限。

        长时间停机后状态允许一次跨越：截止后超过宽限期仍未完成的，
        无论停机前是否被标记过逾期，都直接升级。
        """
        now = self.clock()
        changed: list[EnforcementTask] = []
        for task in self.tasks.values():
            escalate_at = task.deadline + ESCALATION_GRACE
            if task.status in (TaskStatus.PENDING, TaskStatus.OVERDUE) and now > escalate_at:
                task.status = TaskStatus.ESCALATED
                task.assignee_queue = ESCALATION_QUEUE
                task.escalated_at = now
                changed.append(task)
            elif task.status is TaskStatus.PENDING and now > task.deadline:
                task.status = TaskStatus.OVERDUE
                changed.append(task)
        if changed:
            self._touch()
        return changed

    def _maybe_close(self, case: Case) -> None:
        if case.status is not CaseStatus.REVOKED:
            return
        case_tasks = [t for t in self.tasks.values() if t.case_id == case.case_id]
        unsettled = [
            t for t in case_tasks
            if t.status not in (TaskStatus.DONE, TaskStatus.CANCELED)
        ]
        live = [
            p for p in self._placements(case)
            if p.status in (PlacementStatus.ACTIVE, PlacementStatus.SUSPENDED)
        ]
        if not unsettled and not live:
            case.status = CaseStatus.CLOSED
            case.closed_at = self.clock()

    # ------------------------------------------------------------ 申诉

    def file_appeal(self, case_id: str, org_id: str, reason: str) -> Appeal:
        case = self._require(self.cases, case_id, "案件")
        self._require(self.orgs, org_id, "申诉主体")
        if org_id not in self._responsible_orgs(case):
            raise AccessDenied("只有案件责任主体可以提出申诉")
        if case.status not in (CaseStatus.REVOKED, CaseStatus.CLOSED):
            raise DomainError("仅被撤销处置的案件可以申诉")
        open_appeal = next(
            (a for a in self.appeals.values() if a.case_id == case_id and a.status in
             (AppealStatus.FILED, AppealStatus.REVIEWING)),
            None,
        )
        if open_appeal is not None:
            raise DomainError("已有在审申诉")
        appeal = Appeal(
            appeal_id=self._next_id("APL"),
            case_id=case_id,
            filed_by_org_id=org_id,
            filed_at=self.clock(),
            reason=reason,
        )
        self.appeals[appeal.appeal_id] = appeal
        self._touch()
        return appeal

    def resolve_appeal(
        self, appeal_id: str, reviewer: User, overturn: bool, note: str
    ) -> Appeal:
        appeal = self._require(self.appeals, appeal_id, "申诉")
        case = self.cases[appeal.case_id]
        self._require_reviewer(reviewer, case)
        if reviewer.org_id in self._responsible_orgs(case):
            raise AccessDenied("责任关联方不能裁决本案申诉")
        if appeal.status not in (AppealStatus.FILED, AppealStatus.REVIEWING):
            raise DomainError("申诉已裁决")
        now = self.clock()
        appeal.resolved_at = now
        appeal.resolution_note = note
        if overturn:
            appeal.status = AppealStatus.OVERTURNED
            # 撤销尚未执行的处置任务；已下架的投放保持下架，需重新投放
            for task in self.tasks.values():
                if task.case_id == case.case_id and task.status in (
                    TaskStatus.PENDING, TaskStatus.OVERDUE, TaskStatus.ESCALATED
                ):
                    task.status = TaskStatus.CANCELED
                    task.result_note = f"申诉成立撤销：{note}"
            for placement in self._placements(case):
                if placement.status is PlacementStatus.SUSPENDED:
                    placement.status = PlacementStatus.ACTIVE
            case.status = CaseStatus.APPROVED
        else:
            appeal.status = AppealStatus.UPHELD
            self._maybe_close(case)
        self._touch()
        return appeal

    # ------------------------------------------------------------ 证据可见性

    def case_view(self, case_id: str, user: User) -> dict:
        case = self._require(self.cases, case_id, "案件")
        chain = self.trace(case_id)
        if UserRole.LEGAL in user.roles:
            chain["evidence_level"] = "original"  # 法务可查看证据原件
            return chain
        # 普通审核员/运营者只能看到摘要，原件引用被遮蔽
        chain["evidence_level"] = "summary"
        asset = chain["asset"]
        asset["evidence_refs"] = ["<原件仅限法务查看>"]
        if asset.get("persona"):
            asset["persona"]["evidence_refs"] = ["<原件仅限法务查看>"]
            for auth in asset["persona"].get("authorizations", []):
                auth["document_ref"] = "<授权书原件仅限法务查看>"
        return chain

    # ------------------------------------------------------------ 全链路还原

    def trace(self, case_id: str) -> dict:
        case = self._require(self.cases, case_id, "案件")
        asset = self.assets[case.asset_id]
        product = self.products[case.product_id]
        persona = self.identities.get(asset.persona_identity_id) if asset.persona_identity_id else None

        decisions = sorted(
            (d for d in self.decisions.values() if d.case_id == case_id),
            key=lambda d: d.created_at,
        )
        placements = [self.placements[pid] for pid in case.placement_ids]
        case_tasks = sorted(
            (t for t in self.tasks.values() if t.case_id == case_id),
            key=lambda t: t.created_at,
        )
        appeals = sorted(
            (a for a in self.appeals.values() if a.case_id == case_id),
            key=lambda a: a.filed_at,
        )
        scripts = sorted(
            (s for s in self.scripts.values() if s.case_id == case_id),
            key=lambda s: s.created_at,
        )

        return {
            "case": models.to_dict(case),
            "responsible_parties": {
                "merchant": self.orgs[case.merchant_org_id].name,
                "beneficiary": self.orgs[case.beneficiary_org_id].name,
                "producer": self.orgs[asset.producer_org_id].name,
                "registrant": self.orgs[product.registrant_org_id].name,
                "link_controller": self.orgs[product.link_org_id].name,
            },
            "asset": models.to_dict(asset) | {
                "persona": models.to_dict(persona) if persona else None,
            },
            "product": models.to_dict(product),
            "script_versions": [models.to_dict(s) for s in scripts],
            "decisions": [models.to_dict(d) for d in decisions],
            "placements": [models.to_dict(p) for p in placements],
            "enforcement_tasks": [models.to_dict(t) for t in case_tasks],
            "appeals": [models.to_dict(a) for a in appeals],
            "timeline": self._timeline(case, scripts, decisions, placements, case_tasks, appeals),
        }

    def _timeline(self, case, scripts, decisions, placements, tasks, appeals) -> list[dict]:
        # 同一时刻的事件按业务阶段排序，避免同时间戳时次序错乱
        priority = {
            "案件提交": 0, "脚本": 1, "审核结论": 2, "投放": 3,
            "处置任务生成": 4, "申诉提出": 5, "任务完成": 6, "申诉裁决": 7, "案件结案": 8,
        }
        events: list[dict] = []
        events.append({"at": case.created_at, "event": "案件提交", "ref": case.case_id})
        for s in scripts:
            label = "脚本创建" if s.supersedes is None else f"脚本修改（替代 {s.supersedes}）"
            events.append({"at": s.created_at, "event": label, "ref": s.version_id})
        for d in decisions:
            events.append({
                "at": d.created_at,
                "event": f"审核结论：{d.decision.value}（规则包 {d.rule_pack_version}）",
                "ref": d.decision_id,
            })
        for p in placements:
            events.append({"at": p.placed_at, "event": f"投放 {p.channel}", "ref": p.placement_id})
        for t in tasks:
            events.append({"at": t.created_at, "event": f"处置任务生成：{t.task_type.value}", "ref": t.task_id})
            if t.completed_at:
                events.append({"at": t.completed_at, "event": f"任务完成：{t.task_type.value}", "ref": t.task_id})
        for a in appeals:
            events.append({"at": a.filed_at, "event": "申诉提出", "ref": a.appeal_id})
            if a.resolved_at:
                events.append({
                    "at": a.resolved_at,
                    "event": f"申诉裁决：{a.status.value}",
                    "ref": a.appeal_id,
                })
        if case.closed_at:
            events.append({"at": case.closed_at, "event": "案件结案", "ref": case.case_id})

        def order_key(e: dict):
            stage = next((k for k in priority if e["event"].startswith(k) or e["event"] == k), 9)
            return (e["at"], priority.get(stage, 9), e["ref"])

        return [{"at": e["at"].isoformat(), "event": e["event"], "ref": e["ref"]}
                for e in sorted(events, key=order_key)]

    # ------------------------------------------------------------ 辅助

    def _placements(self, case: Case) -> list[Placement]:
        return [self.placements[pid] for pid in case.placement_ids]

    def _active_placements(self, case: Case) -> list[Placement]:
        return [
            p for p in self._placements(case)
            if p.status in (PlacementStatus.ACTIVE, PlacementStatus.SUSPENDED)
        ]

    @staticmethod
    def _require(index: dict, key: str, label: str):
        if key not in index:
            raise DomainError(f"{label}不存在：{key}")
        return index[key]

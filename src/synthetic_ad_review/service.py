"""广告审查服务：提交、扫描、审查、挂起、处置、申诉、时限推进。

所有时间均为绝对时间（UTC ISO）。服务可在任意时刻整体落盘并在新进程中恢复，
整改时限依据落盘的 due_at 继续推进，不依赖内存中的定时器。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Callable

from . import rules
from .access import AccessError, User, require_queue, require_review_access
from .model import (
    AiLabel,
    Case,
    CaseStatus,
    Conflict,
    Decision,
    DigitalPerson,
    Event,
    Party,
    PlacementSnapshot,
    Product,
    ReviewOpinion,
    ReviewState,
    ReviewerRole,
    RiskHit,
    ScriptVersion,
    Severity,
    Task,
    TaskKind,
    TaskStatus,
    content_hash,
    utc_now,
    utc_now_iso,
)

RECTIFY_SLA = timedelta(hours=24)


class ConflictError(Exception):
    """提交/放行时发现主体或商品冲突，案件已挂起调查。"""


class RepostConflictError(ConflictError):
    """同一素材疑似更换发布账号再次投放。"""


def _idempotency_key(asset_id: str, script_hash: str, publisher_account: str) -> str:
    return f"{asset_id}|{script_hash}|{publisher_account}"


class ReviewService:
    def __init__(self, clock: Callable[[], object] = utc_now):
        self.parties: dict[str, Party] = {}
        self.cases: dict[str, Case] = {}
        self.authorizations: dict[str, object] = {}
        self.queue_assignees: dict[str, str] = {}
        # 幂等键（素材|脚本哈希|发布账号）→ 已作出终局结论的审核 ID
        self.decision_index: dict[str, str] = {}
        # 素材台账：素材 → 商品 / AI 标识现状 / 获利方线索，供巡查立案
        self._assets: dict[str, DigitalPerson] = {}
        self._asset_products: dict[str, Product] = {}
        self._asset_labels: dict[str, AiLabel] = {}
        self._asset_beneficiaries: dict[str, str | None] = {}
        self._clock = clock

    # ---------- 时间 ----------
    def now(self):
        return self._clock()

    def now_iso(self) -> str:
        return self.now().isoformat()

    # ---------- 主体登记 ----------
    def register_party(self, party: Party) -> Party:
        self.parties[party.party_id] = party
        return party

    def create_case(
        self,
        case_id: str,
        title: str,
        advertiser_party_id: str,
        product: Product,
        digital_person: DigitalPerson,
        ai_label: AiLabel,
        queue: str,
        beneficiary_party_id: str | None = None,
    ) -> Case:
        case = Case(
            case_id=case_id,
            title=title,
            advertiser_party_id=advertiser_party_id,
            beneficiary_party_id=beneficiary_party_id,
            product=product,
            digital_person=digital_person,
            ai_label=ai_label,
            queue=queue,
        )
        self.cases[case_id] = case
        case.record("system", "case_created", title=title, queue=queue)
        return case

    def add_evidence(self, case_id: str, evidence) -> None:
        case = self._case(case_id)
        case.evidence.append(evidence)
        case.record(evidence.submitted_by_party_id or "system", "evidence_submitted",
                    evidence_id=evidence.evidence_id, kind=evidence.kind)

    # ---------- 脚本提交（幂等 + 改版重审） ----------
    def submit_script(
        self,
        case_id: str,
        text: str,
        author_party_id: str,
        channel: str,
        publisher_account: str,
    ) -> tuple[ScriptVersion, bool]:
        """提交脚本。返回（脚本版本, 是否沿用原结论）。

        - 内容哈希变化即新版本，必须重新审核；
        - 相同 素材+脚本哈希+发布账号 的重复提交沿用原结论（REUSE）。
        """
        case = self._case(case_id)
        h = content_hash(text)
        key = _idempotency_key(case.digital_person.asset_id, h, publisher_account)
        case.idempotency_key = key

        prior_review = self._find_prior_review(key)

        version_no = len(case.scripts) + 1
        script = ScriptVersion(
            version=version_no, hash=h, text=text,
            created_at=self.now_iso(), author_party_id=author_party_id,
        )

        if prior_review is not None:
            # 重复提交：沿用原结论，复制原命中与结论
            prior_script = self._script_of_review(prior_review)
            script.hits = [replace(hit) for hit in prior_script.hits]
            if prior_review.decision is Decision.APPROVE:
                script.review_state = ReviewState.APPROVED
            elif prior_review.decision is Decision.REJECT:
                script.review_state = ReviewState.REJECTED
            else:
                script.review_state = ReviewState.PENDING
            case.scripts.append(script)
            opinion = ReviewOpinion(
                review_id=self._new_review_id(case),
                script_version=version_no,
                reviewer_user="system",
                reviewer_role=ReviewerRole.SYSTEM,
                decision=Decision.REUSE,
                rule_version=prior_review.rule_version,
                reasons=[f"重复提交，沿用审核 {prior_review.review_id} 的原结论："
                         f"{prior_review.decision.value}"] + list(prior_review.reasons),
                created_at=self.now_iso(),
                reused_from_review_id=prior_review.review_id,
            )
            script.review_id = opinion.review_id
            case.reviews.append(opinion)
            case.status = (CaseStatus.APPROVED if prior_review.decision is Decision.APPROVE
                           else CaseStatus.REJECTED)
            case.record("system", "review_reused",
                        review_id=opinion.review_id, reused_from=prior_review.review_id,
                        decision=prior_review.decision.value)
            return script, True

        # 新内容：重新扫描、重新审查
        script.hits = rules.scan(
            category=case.product.category,
            text=text,
            product=case.product,
            digital_person=case.digital_person,
            ai_label=case.ai_label,
            has_valid_identity_auth=self._has_valid_identity_auth(case),
        )
        case.scripts.append(script)
        case.status = CaseStatus.PENDING_REVIEW
        case.record(author_party_id, "script_submitted",
                    version=version_no, hash=h, channel=channel,
                    publisher_account=publisher_account,
                    hits=[f"{h.rule_id}:{h.term}" for h in script.hits])

        self._detect_conflicts(case, channel=channel, publisher_account=publisher_account)
        return script, False

    def _find_prior_review(self, key: str) -> ReviewOpinion | None:
        review_id = self.decision_index.get(key)
        if review_id is None:
            return None
        return self._review_by_id(review_id)

    def _review_by_id(self, review_id: str) -> ReviewOpinion:
        for case in self.cases.values():
            for review in case.reviews:
                if review.review_id == review_id:
                    return review
        raise LookupError(f"审核 {review_id} 已登记但找不到对应记录")

    def _script_of_review(self, review: ReviewOpinion) -> ScriptVersion:
        for case in self.cases.values():
            for script in case.scripts:
                if script.review_id == review.review_id:
                    return script
        raise LookupError(f"找不到审核 {review.review_id} 对应的脚本")

    # ---------- 冲突检测与挂起调查 ----------
    def _detect_conflicts(self, case: Case, channel: str | None = None,
                          publisher_account: str | None = None) -> None:
        """主体或商品冲突：挂起调查。状态置为 UNDER_INVESTIGATION。"""
        new_conflicts: list[Conflict] = []

        if not case.beneficiary_party_id:
            new_conflicts.append(Conflict(
                code="BENEFICIARY_UNKNOWN",
                message="实际获利方未登记：脚本、商品链接、投放账户分属不同主体，收款/结算主体缺失",
                related_party_ids=[case.advertiser_party_id],
            ))
        elif case.beneficiary_party_id not in self.parties:
            new_conflicts.append(Conflict(
                code="BENEFICIARY_UNREGISTERED",
                message=f"实际获利方 {case.beneficiary_party_id} 未完成主体登记",
                related_party_ids=[case.beneficiary_party_id],
            ))

        product = case.product
        if product.approval_no and product.category in rules.APPROVAL_PREFIXES:
            if not rules.approval_prefix_matches(product.category, product.approval_no):
                new_conflicts.append(Conflict(
                    code="APPROVAL_CATEGORY_MISMATCH",
                    message=(f"商品按{product.category.label}送审，但文号 {product.approval_no} "
                             f"与该类别不匹配，疑似套用文号或类别造假"),
                    related_product_id=product.product_id,
                ))
        if product.registrant_party_id and product.registrant_party_id not in self.parties:
            new_conflicts.append(Conflict(
                code="REGISTRANT_UNREGISTERED",
                message=f"商品注册/备案主体 {product.registrant_party_id} 未登记，责任链断裂",
                related_party_ids=[product.registrant_party_id],
                related_product_id=product.product_id,
            ))
        if product.link_owner_party_id and product.link_owner_party_id not in self.parties:
            new_conflicts.append(Conflict(
                code="LINK_OWNER_UNREGISTERED",
                message=f"商品链接控制方 {product.link_owner_party_id} 未登记",
                related_party_ids=[product.link_owner_party_id],
                related_product_id=product.product_id,
            ))

        # 同一素材在其他案件下由不同发布账号投放（含已被下架/驳回后换号复活）
        asset_id = case.digital_person.asset_id
        for other, placement in self._find_cross_account_reposts(
            asset_id, publisher_account or ""
        ):
            new_conflicts.append(Conflict(
                code="REPOST_CROSS_ACCOUNT",
                message=(f"同一素材 {asset_id} 已在案件 {other.case_id} 由账号 "
                         f"{placement.publisher_account} 投放/处置，当前账号 {publisher_account} "
                         f"疑似换账号再次发布"),
                related_party_ids=[placement.publisher_party_id],
            ))
            other.record("system", "repost_detected",
                         by_case=case.case_id, asset_id=asset_id,
                         account=publisher_account)

        if new_conflicts:
            case.conflicts.extend(new_conflicts)
            case.status = CaseStatus.UNDER_INVESTIGATION
            for conflict in new_conflicts:
                case.record("system", "case_suspended",
                            conflict=conflict.code, message=conflict.message)
            raise ConflictError("；".join(c.message for c in new_conflicts))

    def update_advertiser(self, user: User, case_id: str, party_id: str) -> None:
        """调查确认脚本/广告实际控制方后，更正案件广告主主体。"""
        case = self._case(case_id)
        require_review_access(user, case)
        if party_id not in self.parties:
            raise ValueError(f"主体 {party_id} 须先完成登记")
        old = case.advertiser_party_id
        case.advertiser_party_id = party_id
        case.record(user.username, "advertiser_corrected",
                    old_party_id=old, new_party_id=party_id)

    def resolve_conflict(self, user: User, case_id: str, code: str, note: str,
                         beneficiary_party_id: str | None = None) -> Conflict:
        """调查结论：补登记获利方等后解除挂起，案件回到待审核。"""
        case = self._case(case_id)
        require_review_access(user, case)
        targets = [c for c in case.conflicts if c.code == code and not c.resolved]
        if not targets:
            raise ValueError(f"案件没有未决冲突 {code}")
        if code == "BENEFICIARY_UNKNOWN" and beneficiary_party_id:
            if beneficiary_party_id not in self.parties:
                raise ValueError(f"主体 {beneficiary_party_id} 须先完成登记")
            case.beneficiary_party_id = beneficiary_party_id
            # 调查结论反哺素材台账：同素材再次出现时不再缺失获利方
            self._asset_beneficiaries[case.digital_person.asset_id] = beneficiary_party_id
        for conflict in targets:
            conflict.resolved = True
            conflict.resolution_note = note
        case.record(user.username, "conflict_resolved", conflict=code, note=note)
        if not case.open_conflicts():
            case.status = CaseStatus.PENDING_REVIEW
            case.record("system", "case_resumed")
        return targets[0]

    def _find_cross_account_reposts(self, asset_id: str, account: str):
        """返回同素材、异账号、且仍在投或原案已被驳回/下架的投放记录。"""
        result = []
        for other in self.cases.values():
            for placement in other.placements:
                if placement.asset_id != asset_id or placement.publisher_account == account:
                    continue
                if placement.live or other.status in (CaseStatus.TAKEN_DOWN, CaseStatus.REJECTED):
                    result.append((other, placement))
        return result

    # ---------- 身份授权 ----------
    def _has_valid_identity_auth(self, case: Case) -> bool:
        dp = case.digital_person
        if not dp.identity_auth_id:
            return False
        auth = self.authorizations.get(dp.identity_auth_id)
        return auth is not None and auth.on_file and auth.valid_at(self.now().date().isoformat())

    def register_authorization(self, auth) -> None:
        self.authorizations[auth.auth_id] = auth

    # ---------- 审查 ----------
    def review(self, user: User, case_id: str, decision: Decision,
               reasons: list[str] | None = None) -> ReviewOpinion:
        case = self._case(case_id)
        require_review_access(user, case)
        script = case.current_script()
        if script is None:
            raise ValueError("案件尚未提交脚本")
        if script.review_state is ReviewState.APPROVED:
            raise ValueError("当前脚本版本已审核通过；修改脚本后会生成新版本再行审查")
        if decision is Decision.REUSE:
            raise ValueError("REUSE 仅由系统在重复提交时作出")

        reasons = reasons or []
        opinion = ReviewOpinion(
            review_id=self._new_review_id(case),
            script_version=script.version,
            reviewer_user=user.username,
            reviewer_role=user.role,
            decision=decision,
            rule_version=rules.RULEBOOK_VERSION,
            reasons=reasons,
            created_at=self.now_iso(),
            assigned_queue=case.queue,
        )

        if decision is Decision.ESCALATE:
            case.status = CaseStatus.UNDER_INVESTIGATION
            if not case.open_conflicts():
                case.conflicts.append(Conflict(
                    code="REVIEWER_ESCALATION",
                    message="审核员升级调查：" + "；".join(reasons),
                ))
            script.review_id = opinion.review_id
            script.review_state = ReviewState.PENDING
            case.reviews.append(opinion)
            case.record(user.username, "review_escalated", review_id=opinion.review_id)
            return opinion

        if decision is Decision.APPROVE:
            if case.open_conflicts():
                raise AccessError("案件存在未决的主体/商品冲突，处于挂起调查，不得通过")
            blocking = [h for h in script.hits if h.severity in (Severity.BANNED, Severity.REQUIRED)]
            if blocking:
                raise AccessError(
                    "存在禁用/必备项风险命中，不得通过："
                    + "、".join(f"{h.rule_id}({h.term})" for h in blocking)
                )
            restricted = [h for h in script.hits if h.severity is Severity.RESTRICTED]
            if restricted and not any(e.kind == "substantiation" for e in case.evidence):
                raise AccessError(
                    "存在限制性声称，需补交真实体验凭证等证明文件（substantiation）后方可通过"
                )
            case.status = CaseStatus.APPROVED
            script.review_state = ReviewState.APPROVED
        elif decision is Decision.REJECT:
            case.status = CaseStatus.REJECTED
            script.review_state = ReviewState.REJECTED
        else:  # pragma: no cover - 枚举穷尽
            raise ValueError(f"未知审查结论 {decision}")

        script.review_id = opinion.review_id
        case.reviews.append(opinion)
        if case.idempotency_key and decision in (Decision.APPROVE, Decision.REJECT):
            self.decision_index[case.idempotency_key] = opinion.review_id
        case.record(user.username, f"review_{decision.value}",
                    review_id=opinion.review_id, version=script.version,
                    hits=[f"{h.rule_id}:{h.term}" for h in script.hits])

        if decision is Decision.REJECT and case.live_placements():
            self._enforce(case, actor=user.username,
                          reason="审核驳回在投广告：" + "；".join(reasons),
                          hits=script.hits)
        return opinion

    # ---------- 投放 ----------
    def start_placement(self, user: User, case_id: str, channel: str,
                        publisher_account: str, publisher_party_id: str) -> PlacementSnapshot:
        case = self._case(case_id)
        require_queue(user, case.queue)
        script = case.current_script()
        if script is None or script.review_state is not ReviewState.APPROVED:
            raise ValueError("只有审核通过的脚本版本才能投放")
        if publisher_party_id not in self.parties:
            raise ValueError(f"投放账户主体 {publisher_party_id} 未登记")

        # 换账号再发拦截：同一素材已有其他发布主体账号的在投/处置记录
        for other, prior in self._find_cross_account_reposts(
            case.digital_person.asset_id, publisher_account
        ):
            if prior.publisher_party_id == publisher_party_id and prior.live:
                continue  # 同一主体多账号的在投记录不拦（仍在同主体责任下）
            conflict = Conflict(
                code="REPOST_CROSS_ACCOUNT",
                message=(f"同一素材 {prior.asset_id} 曾由 {prior.publisher_account} "
                         f"(案件 {other.case_id}) 投放/处置，当前账号 {publisher_account} 属换账号再发"),
                related_party_ids=[prior.publisher_party_id, publisher_party_id],
            )
            case.conflicts.append(conflict)
            case.status = CaseStatus.UNDER_INVESTIGATION
            case.record(user.username, "placement_blocked_repost",
                        conflict=conflict.code, existing_case=other.case_id)
            other.record("system", "repost_detected",
                         by_case=case.case_id, asset_id=prior.asset_id,
                         account=publisher_account)
            raise RepostConflictError(conflict.message)

        placement = PlacementSnapshot(
            placement_id=f"PL-{len(case.placements) + 1:03d}-{case.case_id}",
            channel=channel,
            publisher_account=publisher_account,
            publisher_party_id=publisher_party_id,
            script_version=script.version,
            script_hash=script.hash,
            asset_id=case.digital_person.asset_id,
            started_at=self.now_iso(),
        )
        case.placements.append(placement)
        case.record(user.username, "placement_started",
                    placement_id=placement.placement_id, channel=channel,
                    account=publisher_account, version=script.version)
        return placement

    # ---------- 处置：下架 / 通知 / 整改 ----------
    def takedown(self, user: User, case_id: str, reason: str,
                 hits: list[RiskHit] | None = None) -> list[Task]:
        case = self._case(case_id)
        require_review_access(user, case)
        if hits is None:
            current = case.current_script()
            hits = current.hits if current else []
        return self._enforce(case, actor=user.username, reason=reason, hits=hits)

    def register_live_placement(
        self, user: User, channel: str, publisher_account: str,
        publisher_party_id: str, asset_id: str, script_text: str,
        observed_at: str | None = None, note: str = "巡查发现视频已在投放",
    ) -> tuple[Case, PlacementSnapshot]:
        """登记巡查中发现的、未经本平台审核即在投放的问题视频（如白大褂药膏视频）。

        视频来源、投放账号、脚本原文先行存证；脚本立即扫描并挂入待处置队列，
        随后可直接 takedown，不必等送审流程。
        """
        if publisher_party_id not in self.parties:
            raise ValueError(f"投放账户主体 {publisher_party_id} 须先登记")
        case_id = f"CASE-PATROL-{asset_id}-{publisher_account}"
        if case_id in self.cases:
            raise ValueError(f"该投放已登记为案件 {case_id}")

        asset = self._assets.get(asset_id) if hasattr(self, "_assets") else None
        if asset is None:
            raise LookupError(f"素材 {asset_id} 未登记，无法确认生成来源")
        product = self._asset_products.get(asset_id) if hasattr(self, "_asset_products") else None
        if product is None:
            raise LookupError(f"素材 {asset_id} 未关联商品")

        case = self.create_case(
            case_id=case_id,
            title=f"巡查在投视频：{asset.name} @ {publisher_account}",
            advertiser_party_id=publisher_party_id,
            product=product,
            digital_person=asset,
            ai_label=self._asset_labels.get(asset_id, AiLabel()),
            queue="patrol",
            beneficiary_party_id=self._asset_beneficiaries.get(asset_id),
        )
        case.queue = "patrol"
        snapshot = PlacementSnapshot(
            placement_id=f"PL-001-{case_id}",
            channel=channel,
            publisher_account=publisher_account,
            publisher_party_id=publisher_party_id,
            script_version=1,
            script_hash=content_hash(script_text),
            asset_id=asset_id,
            started_at=observed_at or self.now_iso(),
        )
        case.placements.append(snapshot)
        case.record(user.username, "live_placement_observed",
                    channel=channel, account=publisher_account, note=note)

        script = ScriptVersion(
            version=1, hash=snapshot.script_hash, text=script_text,
            created_at=self.now_iso(), author_party_id=publisher_party_id,
        )
        script.hits = rules.scan(
            category=product.category, text=script_text, product=product,
            digital_person=asset, ai_label=case.ai_label,
            has_valid_identity_auth=self._has_valid_identity_auth(case),
        )
        case.scripts.append(script)
        case.status = CaseStatus.PENDING_REVIEW
        case.record("system", "script_submitted", version=1, hash=script.hash,
                    channel=channel, publisher_account=publisher_account,
                    origin="patrol",
                    hits=[f"{h.rule_id}:{h.term}" for h in script.hits])
        try:
            self._detect_conflicts(case, channel=channel, publisher_account=publisher_account)
        except ConflictError:
            if any(c.code == "REPOST_CROSS_ACCOUNT" for c in case.open_conflicts()):
                raise RepostConflictError("；".join(c.message for c in case.open_conflicts()))
            raise
        return case, snapshot

    def register_asset(self, digital_person: DigitalPerson, product: Product,
                       ai_label: AiLabel, beneficiary_party_id: str | None = None) -> None:
        """素材台账：素材与商品、AI 标识现状、获利方线索的对应关系，供巡查立案使用。"""
        if not hasattr(self, "_assets"):
            self._assets, self._asset_products, self._asset_labels = {}, {}, {}
            self._asset_beneficiaries = {}
        self._assets[digital_person.asset_id] = digital_person
        self._asset_products[digital_person.asset_id] = product
        self._asset_labels[digital_person.asset_id] = ai_label
        self._asset_beneficiaries[digital_person.asset_id] = beneficiary_party_id

    def relabel_current_asset(self, case_id: str, ai_label: AiLabel) -> ScriptVersion:
        """待审素材补齐 AI 标识后，对当前脚本重新扫描（脚本本身未改版）。"""
        case = self._case(case_id)
        case.ai_label = ai_label
        script = case.current_script()
        if script is None:
            raise ValueError("案件尚无脚本")
        script.hits = rules.scan(
            category=case.product.category, text=script.text, product=case.product,
            digital_person=case.digital_person, ai_label=ai_label,
            has_valid_identity_auth=self._has_valid_identity_auth(case),
        )
        case.record("system", "ai_label_updated",
                    hits=[f"{h.rule_id}:{h.term}" for h in script.hits])
        return script

    def _enforce(self, case: Case, actor: str, reason: str,
                 hits: list[RiskHit]) -> list[Task]:
        case.status = CaseStatus.TAKEN_DOWN
        created: list[Task] = []
        live = case.live_placements()

        if live:
            takedown = Task(
                task_id=self._new_task_id(case, TaskKind.TAKEDOWN),
                case_id=case.case_id, kind=TaskKind.TAKEDOWN,
                assignee_user=self._queue_assignee(case.queue),
                note=reason,
            )
            case.tasks.append(takedown)
            created.append(takedown)

        notify = Task(
            task_id=self._new_task_id(case, TaskKind.NOTIFY),
            case_id=case.case_id, kind=TaskKind.NOTIFY,
            assignee_user=self._queue_assignee(case.queue),
            note=f"通知广告主/发布者/获利方：{reason}",
        )
        case.tasks.append(notify)
        created.append(notify)
        case.record(actor, "enforcement_issued",
                    reason=reason, placements=[p.placement_id for p in live],
                    tasks=[t.task_id for t in created])

        # 处置一律配套限期整改（24 小时绝对时限）；禁用词整改=改脚本并按新版本重审
        rectify_note = (
            "限期整改：" + "、".join(sorted({h.term for h in hits}))
            if hits else "配合调查并限期整改，整改后按新脚本版本重新送审"
        )
        rectify = Task(
            task_id=self._new_task_id(case, TaskKind.RECTIFY),
            case_id=case.case_id, kind=TaskKind.RECTIFY,
            assignee_user=self._queue_assignee(case.queue),
            due_at=(self.now() + RECTIFY_SLA).isoformat(),
            note=rectify_note,
        )
        case.tasks.append(rectify)
        created.append(rectify)
        case.record(actor, "rectify_deadline_set",
                    task_id=rectify.task_id, due_at=rectify.due_at)
        return created

    def _queue_assignee(self, queue: str) -> str | None:
        return self.queue_assignees.get(queue)

    def set_queue_assignee(self, queue: str, username: str) -> None:
        if not hasattr(self, "queue_assignees"):
            self.queue_assignees = {}
        self.queue_assignees[queue] = username

    # ---------- 任务执行（运营者只能处理自己队列/自己的任务） ----------
    def complete_task(self, user: User, task_id: str, note: str | None = None) -> Task:
        task = self._find_task(task_id)
        case = self._case(task.case_id)
        require_queue(user, case.queue)
        if task.assignee_user and task.assignee_user != user.username:
            raise AccessError(f"任务 {task_id} 分配给 {task.assignee_user}，{user.username} 无权处理")
        if task.status is not TaskStatus.OPEN:
            raise ValueError(f"任务 {task_id} 已{task.status.value}")
        task.status = TaskStatus.DONE
        task.completed_at = self.now_iso()
        if note:
            task.note = (task.note + " | " if task.note else "") + note

        if task.kind is TaskKind.TAKEDOWN:
            for placement in case.live_placements():
                placement.live = False
                placement.ended_at = self.now_iso()
            case.record(user.username, "taken_down", task_id=task_id,
                        placements=[p.placement_id for p in case.placements])
        else:
            case.record(user.username, "task_completed", task_id=task_id, kind=task.kind.value)
        return task

    # ---------- 申诉 ----------
    def file_appeal(self, case_id: str, by_party_id: str, reason: str) -> Task:
        case = self._case(case_id)
        takedown_task = next(
            (t for t in case.tasks if t.kind is TaskKind.TAKEDOWN), None
        )
        appeal = Task(
            task_id=self._new_task_id(case, TaskKind.APPEAL),
            case_id=case_id, kind=TaskKind.APPEAL,
            assignee_user=self._queue_assignee(case.queue),
            note=f"{by_party_id} 申诉：{reason}",
            linked_task_id=takedown_task.task_id if takedown_task else None,
        )
        case.tasks.append(appeal)
        case.record(by_party_id, "appeal_filed", task_id=appeal.task_id, reason=reason)
        return appeal

    def decide_appeal(self, user: User, task_id: str, uphold: bool, note: str) -> Task:
        task = self._find_task(task_id)
        case = self._case(task.case_id)
        require_review_access(user, case)
        if task.kind is not TaskKind.APPEAL or task.status is not TaskStatus.OPEN:
            raise ValueError("只能对进行中的申诉任务作出裁决")
        task.status = TaskStatus.DONE
        task.completed_at = self.now_iso()
        task.note = (task.note or "") + f" | 裁决：{'维持' if uphold else '撤销'}；{note}"

        if uphold:
            case.record(user.username, "appeal_upheld", task_id=task_id, note=note)
        else:
            # 申诉成立：撤销未执行的下架/通知，恢复在投状态
            for linked in case.tasks:
                if (linked.kind in (TaskKind.TAKEDOWN, TaskKind.NOTIFY, TaskKind.RECTIFY)
                        and linked.status is TaskStatus.OPEN):
                    linked.status = TaskStatus.CANCELLED
            for placement in case.placements:
                if not placement.live and placement.ended_at is not None:
                    placement.live = True
                    placement.ended_at = None
            case.status = CaseStatus.APPROVED
            case.record(user.username, "appeal_overturned", task_id=task_id,
                        note=note, restored_placements=[p.placement_id for p in case.placements])
        return task

    # ---------- 时限推进（服务恢复后继续计算） ----------
    def advance_deadlines(self) -> list[Event]:
        """扫描全部未结任务；整改逾期自动升级为下架，并对每个逾期任务记录一次事件。

        仅依赖落盘的绝对 due_at：服务停机多久，恢复后一次性补齐推进。
        """
        events: list[Event] = []
        now = self.now()
        for case in self.cases.values():
            overdue = [t for t in case.tasks if t.is_overdue(now)]
            for task in overdue:
                seq_before = len(case.events)
                already = any(
                    e.action == "task_overdue" and e.detail.get("task_id") == task.task_id
                    for e in case.events
                )
                if already:
                    continue
                case.record("system", "task_overdue",
                            task_id=task.task_id, kind=task.kind.value,
                            due_at=task.due_at)
                if task.kind is TaskKind.RECTIFY and task.status is TaskStatus.OPEN:
                    self._escalate_rectify(case, task)
                events.extend(case.events[seq_before:])
        return events

    def _escalate_rectify(self, case: Case, rectify: Task) -> None:
        """整改逾期：系统强制执行下架并关闭相关任务，确保在投版本下线。"""
        for placement in case.live_placements():
            placement.live = False
            placement.ended_at = self.now_iso()
        for task in case.tasks:
            if (task.kind is TaskKind.TAKEDOWN and task.status is TaskStatus.OPEN
                    and any(p for p in case.placements)):
                task.status = TaskStatus.DONE
                task.completed_at = self.now_iso()
                task.note = (task.note or "") + f" | 整改 {rectify.task_id} 逾期，系统强制执行"
        rectify.status = TaskStatus.CANCELLED
        case.record("system", "rectify_overdue_escalated",
                    rectify_task_id=rectify.task_id,
                    placements=[p.placement_id for p in case.placements])

    # ---------- 证据可见性 ----------
    def get_evidence_view(self, user: User, case_id: str, evidence_id: str) -> dict:
        from .access import view_evidence
        case = self._case(case_id)
        evidence = next((e for e in case.evidence if e.evidence_id == evidence_id), None)
        if evidence is None:
            raise LookupError(f"证据 {evidence_id} 不存在")
        return view_evidence(user, evidence)

    # ---------- 辅助 ----------
    def _case(self, case_id: str) -> Case:
        if case_id not in self.cases:
            raise LookupError(f"案件 {case_id} 不存在")
        return self.cases[case_id]

    def _find_task(self, task_id: str) -> Task:
        for case in self.cases.values():
            for task in case.tasks:
                if task.task_id == task_id:
                    return task
        raise LookupError(f"任务 {task_id} 不存在")

    def _new_review_id(self, case: Case) -> str:
        return f"RV-{len(case.reviews) + 1:03d}-{case.case_id}"

    def _new_task_id(self, case: Case, kind: TaskKind) -> str:
        count = sum(1 for t in case.tasks if t.kind is kind) + 1
        return f"TK-{kind.value[:2].upper()}-{count:03d}-{case.case_id}"

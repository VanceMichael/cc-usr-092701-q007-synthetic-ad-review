"""审查服务端到端业务测试：以白大褂数字人药膏场景为主线。"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

from src.synthetic_ad_review.models import (
    AILabelStatus,
    AppealStatus,
    Asset,
    AssetType,
    AuthorizationStatus,
    CaseStatus,
    DecisionType,
    IdentityAuthorization,
    Org,
    OrgRole,
    PersonaIdentity,
    PlacementStatus,
    Product,
    ProductCategory,
    Profession,
    Severity,
    TaskStatus,
    TaskType,
    User,
    UserRole,
)
from src.synthetic_ad_review.rules import evaluate
from src.synthetic_ad_review.service import AccessDenied, DomainError, ReviewService
from src.synthetic_ad_review.store import Store

BASE = datetime(2026, 9, 27, 9, 0)


class Clock:
    def __init__(self, start=BASE):
        self.now = start

    def __call__(self):
        return self.now


class Scenario:
    """白大褂数字人宣称药膏断根的演示场景。"""

    def __init__(self, svc: ReviewService):
        self.svc = svc
        # 康民制药：广告主兼药品备案主体
        self.merchant = svc.register_org(Org("org-kangmin", "康民制药有限公司", {OrgRole.MERCHANT}))
        # 幻影数字：素材生成方（制作机构）
        self.producer = svc.register_org(Org("org-huanying", "幻影数字科技有限公司", {OrgRole.PRODUCER}))
        # 汇通电商：控制商品链接与投放账户
        self.shop = svc.register_org(Org("org-huitong", "汇通电子商务有限公司", {OrgRole.PUBLISHER}))
        # 辰光咨询：隐蔽的实际获利方
        self.beneficiary = svc.register_org(Org("org-chenguang", "辰光信息咨询有限公司", {OrgRole.BENEFICIARY}))
        # 平台：审核员与运营者所在主体
        self.platform = svc.register_org(Org("org-platform", "平台运营方", {OrgRole.PUBLISHER}))

        self.independent_reviewer = User("u-rev", "周审核", self.platform.org_id, {UserRole.REVIEWER})
        self.merchant_reviewer = User("u-rev-m", "钱自检", self.merchant.org_id, {UserRole.REVIEWER})
        self.legal = User("u-legal", "孙法务", self.platform.org_id, {UserRole.LEGAL})
        self.operator = User("u-ops", "李运营", self.platform.org_id,
                             {UserRole.OPERATOR}, {"ops:短视频"})
        for u in (self.independent_reviewer, self.merchant_reviewer, self.legal, self.operator):
            svc.register_user(u)

        self.auth = IdentityAuthorization(
            "auth-1", "李医生本人", self.producer.org_id, "*",
            BASE - timedelta(days=30), BASE + timedelta(days=300),
            "evidence/auth-li.pdf",
        )
        self.persona = PersonaIdentity(
            "pid-li", "李医生", Profession.MEDICAL_STAFF, real_person=True,
            owner_org_id=self.producer.org_id,
            evidence_refs=["evidence/license-li.jpg"],
            authorizations=[self.auth],
        )
        svc.register_identity(self.persona)

        self.asset = Asset(
            "ast-1", AssetType.DIGITAL_HUMAN_VIDEO, self.producer.org_id,
            generator_tool="幻影数字人引擎 v3",
            generated_at=BASE - timedelta(hours=2),
            ai_label=AILabelStatus.UNLABELED,
            content_hash="hash-video-1",
            summary="穿白大褂的中年男性数字人，背景为诊室，口播药膏功效",
            persona_identity_id=self.persona.identity_id,
            evidence_refs=["evidence/video-ast-1.mp4"],
        )
        svc.register_asset(self.asset)

        self.product = Product(
            "prd-gaoyao", "康民牌消痛膏药", ProductCategory.DRUG,
            registrant_org_id=self.merchant.org_id,
            approval_no="国药准字Z00000001",
            link_org_id=self.shop.org_id,
            link_url="https://shop.example/item/1",
        )
        svc.register_product(self.product)

        self.bad_script = "这款康民牌消痛药膏可以断根，永不复发，李医生推荐大家购买！"
        self.clean_script = "康民牌消痛膏药，请按药品说明书或在药师指导下购买和使用。"

    def submit(self, script=None, merchant_org_id=None, account_org_id=None,
               beneficiary_org_id=None, channel="短视频"):
        return self.svc.submit_case(
            title="白大褂数字人口播药膏",
            asset_id=self.asset.asset_id,
            product_id=self.product.product_id,
            merchant_org_id=merchant_org_id or self.merchant.org_id,
            beneficiary_org_id=beneficiary_org_id or self.beneficiary.org_id,
            script_content=script or self.bad_script,
            channel=channel,
            account_org_id=account_org_id or self.shop.org_id,
            account_name="汇通旗舰店",
        )

    def make_compliant(self):
        """合规药品广告：去掉代言人物、补齐AI标识、链接与备案一致。"""
        self.asset.persona_identity_id = None
        self.asset.ai_label = AILabelStatus.LABELED
        self.product.link_org_id = self.merchant.org_id


def make_service(tmpdir, clock=None, autosave=True):
    clock = clock or Clock()
    return ReviewService(Store(Path(tmpdir) / "state.json"), clock=clock, autosave=autosave)


class RuleEngineTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service(tempfile.mkdtemp(), autosave=False)
        self.scn = Scenario(self.svc)

    def _eval(self, content, category=ProductCategory.DRUG, persona=True,
              ai_label=AILabelStatus.UNLABELED, real_person=True):
        product = Product("p", "试品", category, "org-kangmin", "文号",
                          "org-kangmin")
        asset = Asset("a", AssetType.DIGITAL_HUMAN_VIDEO, "org-huanying", "工具",
                      BASE, ai_label, "h",
                      persona_identity_id="pid-li" if persona else None)
        pid = self.scn.persona if persona else None
        if pid is not None:
            pid.real_person = real_person
        case = self.scn.submit(self.scn.clean_script)  # 仅用于产生脚本版本
        script = self.svc.scripts[case.current_script_version_id]
        script.content = content
        return evaluate(script, product, asset, pid,
                        AuthorizationStatus.VALID if persona else None)

    def test_drug_cure_words_and_medical_endorser(self):
        ev = self._eval(self.scn.bad_script)
        rule_ids = {h.rule_id for h in ev.hits}
        self.assertIn("KW-CURE-001", rule_ids)       # 断根
        self.assertIn("KW-ABS-001", rule_ids)        # 永不复发
        self.assertIn("ID-MED-001", rule_ids)        # 医务人员代言药品
        self.assertIn("AI-LBL-001", rule_ids)        # 缺AI标识
        self.assertTrue(ev.blocked)

    def test_drug_non_medical_endorser_still_blocked(self):
        self.scn.persona.profession = Profession.CONSUMER
        ev = self._eval("普通患者使用后感觉不错，推荐给大家")
        self.assertIn("ID-END-001", {h.rule_id for h in ev.hits})

    def test_health_food_disease_claim_blocked(self):
        ev = self._eval("本品可以治疗高血压", category=ProductCategory.HEALTH_FOOD)
        self.assertIn("KW-DIS-001", {h.rule_id for h in ev.hits})
        self.assertIn("HF-DISC-001", {h.rule_id for h in ev.hits})  # 缺提示语仅警告
        self.assertTrue(ev.blocked)

    def test_health_food_replace_medicine_blocked(self):
        ev = self._eval("本品不能代替药物，但可辅助治疗",
                        category=ProductCategory.HEALTH_FOOD)
        self.assertIn("KW-HF-001", {h.rule_id for h in ev.hits})

    def test_cosmetic_absolute_term_warns_but_not_blocks(self):
        ev = self._eval("国家级护肤配方", category=ProductCategory.COSMETIC,
                        persona=False, ai_label=AILabelStatus.LABELED)
        hits = {h.rule_id: h for h in ev.hits}
        self.assertEqual(hits["KW-ABS-001"].severity, Severity.WARN)
        self.assertFalse(ev.blocked)

    def test_fake_medical_persona_non_drug(self):
        ev = self._eval("李医生说这款保健品特别好",
                        category=ProductCategory.HEALTH_FOOD, real_person=False)
        self.assertIn("ID-FAKE-001", {h.rule_id for h in ev.hits})

    def test_labeled_ai_video_passes_label_rule(self):
        ev = self._eval(self.scn.clean_script, ai_label=AILabelStatus.LABELED)
        self.assertNotIn("AI-LBL-001", {h.rule_id for h in ev.hits})

    def test_authorization_states(self):
        case = self.scn.submit(self.scn.clean_script)
        script = self.svc.scripts[case.current_script_version_id]
        args = (self.scn.product, self.scn.asset, self.scn.persona)
        self.assertEqual(
            self.svc._authorization_status(self.scn.persona, self.scn.asset, BASE),
            AuthorizationStatus.VALID,
        )
        self.scn.persona.authorizations = []
        self.assertIs(
            self.svc._authorization_status(self.scn.persona, self.scn.asset, BASE),
            AuthorizationStatus.MISSING,
        )
        self.scn.persona.authorizations = [IdentityAuthorization(
            "auth-x", "李医生", self.scn.producer.org_id, "*",
            BASE - timedelta(days=400), BASE - timedelta(days=10), "doc")]
        self.assertIs(
            self.svc._authorization_status(self.scn.persona, self.scn.asset, BASE),
            AuthorizationStatus.EXPIRED,
        )
        self.scn.persona.authorizations = [IdentityAuthorization(
            "auth-y", "李医生", self.scn.producer.org_id, "image",
            BASE - timedelta(days=1), BASE + timedelta(days=10), "doc")]
        self.assertIs(
            self.svc._authorization_status(self.scn.persona, self.scn.asset, BASE),
            AuthorizationStatus.SCOPE_MISMATCH,
        )


class SubmitConflictTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = make_service(self.dir, autosave=False)
        self.scn = Scenario(self.svc)

    def test_submission_suspended_on_subject_conflicts(self):
        case = self.scn.submit()
        # 链接控制方≠备案主体，获利方≠广告主/备案主体
        self.assertIs(case.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertEqual(len(case.conflict_notes), 2)

    def test_same_asset_new_account_detected(self):
        first = self.scn.submit(beneficiary_org_id=self.scn.merchant.org_id)
        self.assertIs(first.status, CaseStatus.UNDER_INVESTIGATION)  # 链接冲突仍挂起
        self.svc.resolve_conflict(first.case_id, self.scn.independent_reviewer,
                                  proceed=True, note="链接代运营协议已核实")
        # 同一素材换一家广告主/账户再发
        second = self.scn.submit(
            merchant_org_id=self.scn.shop.org_id,
            beneficiary_org_id=self.scn.merchant.org_id,
            account_org_id=self.scn.merchant.org_id,
        )
        self.assertIs(second.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertTrue(any("换号发布" in n for n in second.conflict_notes))

    def test_resolve_conflict_proceed_or_reject(self):
        case = self.scn.submit()
        self.svc.resolve_conflict(case.case_id, self.scn.independent_reviewer,
                                  proceed=False, note="无法解释获利关系")
        self.assertIs(case.status, CaseStatus.REJECTED)

    def test_duplicate_submission_reuses_case(self):
        case = self.scn.submit()
        again = self.scn.submit()
        self.assertEqual(again.case_id, case.case_id)


class ReviewPermissionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = make_service(self.dir, autosave=False)
        self.scn = Scenario(self.svc)
        # 构造无冲突且合规的广告：无代言、有标识、链接与备案一致
        self.scn.make_compliant()

    def _clean_case(self, script=None):
        case = self.scn.submit(
            script=script or self.scn.clean_script,
            beneficiary_org_id=self.scn.merchant.org_id,
        )
        return case

    def test_independent_reviewer_can_approve_clean_case(self):
        case = self._clean_case()
        decision = self.svc.review_case(
            case.case_id, self.scn.independent_reviewer,
            DecisionType.APPROVE, "材料齐全")
        self.assertIs(case.status, CaseStatus.APPROVED)
        self.assertEqual(decision.hits, [])

    def test_merchant_cannot_approve_own_ad(self):
        case = self._clean_case()
        with self.assertRaises(AccessDenied):
            self.svc.review_case(case.case_id, self.scn.merchant_reviewer,
                                 DecisionType.APPROVE, "自批")
        # 商品方审核员驳回也不允许（任何裁决都需回避）
        with self.assertRaises(AccessDenied):
            self.svc.review_case(case.case_id, self.scn.merchant_reviewer,
                                 DecisionType.REJECT, "自驳")

    def test_blocked_hits_cannot_be_approved(self):
        case = self._clean_case(script=self.scn.bad_script)
        with self.assertRaisesRegex(DomainError, "阻断级"):
            self.svc.review_case(case.case_id, self.scn.independent_reviewer,
                                 DecisionType.APPROVE, "强行通过")
        decision = self.svc.review_case(case.case_id, self.scn.independent_reviewer,
                                        DecisionType.REJECT, "违禁用语")
        self.assertIs(case.status, CaseStatus.REJECTED)
        self.assertTrue(any(h.rule_id == "KW-CURE-001" for h in decision.hits))

    def test_non_reviewer_role_rejected(self):
        case = self._clean_case()
        with self.assertRaises(AccessDenied):
            self.svc.review_case(case.case_id, self.scn.legal,
                                 DecisionType.APPROVE, "法务不能代审核")

    def test_duplicate_after_approval_keeps_decision(self):
        case = self._clean_case()
        self.svc.review_case(case.case_id, self.scn.independent_reviewer,
                             DecisionType.APPROVE, "通过")
        again = self.scn.submit(
            script=self.scn.clean_script,
            beneficiary_org_id=self.scn.merchant.org_id,
        )
        self.assertEqual(again.case_id, case.case_id)
        self.assertIs(again.status, CaseStatus.APPROVED)
        decisions = [d for d in self.svc.decisions.values() if d.case_id == case.case_id]
        self.assertEqual(len(decisions), 1)  # 沿用原结论，未重复审核


class ScriptRevisionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = make_service(self.dir, autosave=False)
        self.scn = Scenario(self.svc)
        self.scn.make_compliant()

    def test_modified_script_forces_new_version_and_rereview(self):
        case = self.scn.submit(script=self.scn.clean_script,
                               beneficiary_org_id=self.scn.merchant.org_id)
        v1 = case.current_script_version_id
        self.svc.review_case(case.case_id, self.scn.independent_reviewer,
                             DecisionType.APPROVE, "v1通过")
        self.assertIs(case.status, CaseStatus.APPROVED)

        v2 = self.svc.revise_script(case.case_id, "加一句：药膏可以断根！",
                                    self.scn.merchant.org_id)
        self.assertNotEqual(v2.version_id, v1)
        self.assertEqual(v2.supersedes, v1)
        self.assertIs(case.status, CaseStatus.PENDING_REVIEW)  # 必须重审
        with self.assertRaisesRegex(DomainError, "阻断级"):
            self.svc.review_case(case.case_id, self.scn.independent_reviewer,
                                 DecisionType.APPROVE, "试图通过违禁版本")

    def test_unchanged_content_reuses_version(self):
        case = self.scn.submit(script=self.scn.clean_script,
                               beneficiary_org_id=self.scn.merchant.org_id)
        v1 = case.current_script_version_id
        same = self.svc.revise_script(case.case_id, self.scn.clean_script,
                                      self.scn.merchant.org_id)
        self.assertEqual(same.version_id, v1)


class PlacementEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.svc = make_service(self.dir, clock=self.clock, autosave=False)
        self.scn = Scenario(self.svc)
        self.scn.make_compliant()
        self.case = self.scn.submit(script=self.scn.clean_script,
                                    beneficiary_org_id=self.scn.merchant.org_id)
        self.svc.review_case(self.case.case_id, self.scn.independent_reviewer,
                             DecisionType.APPROVE, "通过")

    def test_only_approved_case_can_place(self):
        fresh = self.scn.submit(script="另一则合规内容，请按说明书使用。",
                                beneficiary_org_id=self.scn.merchant.org_id,
                                account_org_id=self.scn.merchant.org_id)
        with self.assertRaises(DomainError):
            self.svc.place(fresh.case_id, channel="短视频",
                           account_org_id=self.scn.shop.org_id, account_name="x")

    def test_revoke_spawns_takedown_notify_rectify_tasks(self):
        placement = self.svc.place(
            self.case.case_id, channel="短视频",
            account_org_id=self.scn.shop.org_id, account_name="汇通旗舰店")
        # 已投放版本被固定记录，事后脚本修改不影响追溯
        self.assertEqual(placement.script_version_id, self.case.current_script_version_id)

        self.svc.revoke(self.case.case_id, self.scn.independent_reviewer,
                        reason="巡检发现隐性违禁")
        self.assertIs(self.case.status, CaseStatus.REVOKED)
        self.assertIs(placement.status, PlacementStatus.SUSPENDED)
        tasks = [t for t in self.svc.tasks.values() if t.case_id == self.case.case_id]
        self.assertEqual({t.task_type for t in tasks},
                         {TaskType.TAKEDOWN, TaskType.NOTIFY, TaskType.RECTIFY})
        self.assertTrue(all(t.assignee_queue == "ops:短视频" for t in tasks))

    def test_queue_isolation_and_completion(self):
        self.svc.place(self.case.case_id, channel="短视频",
                       account_org_id=self.scn.shop.org_id, account_name="汇通旗舰店")
        self.svc.revoke(self.case.case_id, self.scn.independent_reviewer, reason="巡检")
        mine = self.svc.queue_tasks(self.scn.operator)
        self.assertEqual(len(mine), 3)

        other_operator = User("u-ops2", "赵运营", self.scn.platform.org_id,
                              {UserRole.OPERATOR}, {"ops:直播"})
        self.svc.register_user(other_operator)
        self.assertEqual(self.svc.queue_tasks(other_operator), [])

        takedown = next(t for t in mine if t.task_type is TaskType.TAKEDOWN)
        with self.assertRaises(AccessDenied):
            self.svc.complete_task(takedown.task_id, other_operator, "越权下架")
        done = self.svc.complete_task(takedown.task_id, self.scn.operator, "链接已删除")
        self.assertIs(done.status, TaskStatus.DONE)
        placement = self.svc.placements[self.case.placement_ids[0]]
        self.assertIs(placement.status, PlacementStatus.REMOVED)
        self.assertIsNotNone(placement.removed_at)

    def test_case_closes_after_all_tasks_done(self):
        self.svc.place(self.case.case_id, channel="短视频",
                       account_org_id=self.scn.shop.org_id, account_name="汇通旗舰店")
        self.svc.revoke(self.case.case_id, self.scn.independent_reviewer, reason="巡检")
        for task in self.svc.queue_tasks(self.scn.operator):
            self.svc.complete_task(task.task_id, self.scn.operator, "完成")
        self.assertIs(self.case.status, CaseStatus.CLOSED)
        self.assertIsNotNone(self.case.closed_at)


class DeadlineRestartTest(unittest.TestCase):
    def test_overdue_and_escalation_survive_restart(self):
        dir_ = tempfile.mkdtemp()
        clock = Clock()
        svc = make_service(dir_, clock=clock, autosave=True)
        scn = Scenario(svc)
        scn.make_compliant()
        case = scn.submit(script=scn.clean_script, beneficiary_org_id=scn.merchant.org_id)
        svc.review_case(case.case_id, scn.independent_reviewer, DecisionType.APPROVE, "通过")
        svc.place(case.case_id, channel="短视频",
                  account_org_id=scn.shop.org_id, account_name="汇通旗舰店")
        svc.revoke(case.case_id, scn.independent_reviewer, reason="巡检")
        rectify = next(t for t in svc.tasks.values() if t.task_type is TaskType.RECTIFY)
        deadline = rectify.deadline

        # 服务停机 25 小时后恢复：下架/通知（24h）应判逾期，整改（72h）不变
        clock.now += timedelta(hours=25)
        restored = make_service(dir_, clock=clock, autosave=True)
        r_rectify = restored.tasks[rectify.task_id]
        self.assertEqual(r_rectify.deadline, deadline)  # 时限是绝对时间，不顺延
        self.assertIs(r_rectify.status, TaskStatus.PENDING)
        overdue = [t for t in restored.tasks.values() if t.status is TaskStatus.OVERDUE]
        self.assertEqual(len(overdue), 2)

        # 再恢复到逾期超过宽限期：自动升级到 escalation 队列
        clock.now = deadline + timedelta(hours=25)
        restored2 = make_service(dir_, clock=clock, autosave=True)
        r2 = restored2.tasks[rectify.task_id]
        self.assertIs(r2.status, TaskStatus.ESCALATED)
        self.assertEqual(r2.assignee_queue, "escalation")
        self.assertIsNotNone(r2.escalated_at)
        # 原队列运营者已看不到该任务
        self.assertNotIn(r2.task_id, {t.task_id for t in restored2.queue_tasks(scn.operator)})


class AppealTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = make_service(self.dir, autosave=False)
        self.scn = Scenario(self.svc)
        self.scn.make_compliant()
        self.case = self.scn.submit(script=self.scn.clean_script,
                                    beneficiary_org_id=self.scn.merchant.org_id)
        self.svc.review_case(self.case.case_id, self.scn.independent_reviewer,
                             DecisionType.APPROVE, "通过")
        self.svc.place(self.case.case_id, channel="短视频",
                       account_org_id=self.scn.shop.org_id, account_name="汇通旗舰店")
        self.svc.revoke(self.case.case_id, self.scn.independent_reviewer, reason="抽检存疑")

    def test_appeal_overturn_cancels_tasks_and_restores(self):
        appeal = self.svc.file_appeal(self.case.case_id, self.scn.merchant.org_id,
                                      "复检合格，请求恢复")
        resolved = self.svc.resolve_appeal(
            appeal.appeal_id, self.scn.independent_reviewer,
            overturn=True, note="抽检误判")
        self.assertIs(resolved.status, AppealStatus.OVERTURNED)
        self.assertIs(self.case.status, CaseStatus.APPROVED)
        tasks = [t for t in self.svc.tasks.values() if t.case_id == self.case.case_id]
        self.assertTrue(all(t.status is TaskStatus.CANCELED for t in tasks))
        placement = self.svc.placements[self.case.placement_ids[0]]
        self.assertIs(placement.status, PlacementStatus.ACTIVE)

    def test_appeal_upheld_keeps_enforcement(self):
        appeal = self.svc.file_appeal(self.case.case_id, self.scn.merchant.org_id, "误判")
        with self.assertRaises(AccessDenied):
            self.svc.resolve_appeal(appeal.appeal_id, self.scn.merchant_reviewer,
                                    overturn=False, note="利益相关")
        resolved = self.svc.resolve_appeal(
            appeal.appeal_id, self.scn.independent_reviewer,
            overturn=False, note="违规事实清楚")
        self.assertIs(resolved.status, AppealStatus.UPHELD)
        tasks = [t for t in self.svc.tasks.values() if t.case_id == self.case.case_id]
        self.assertTrue(all(t.status is TaskStatus.PENDING for t in tasks))

    def test_outside_party_cannot_appeal(self):
        with self.assertRaises(AccessDenied):
            self.svc.file_appeal(self.case.case_id, self.scn.platform.org_id, "与我无关")


class EvidenceAndTraceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = make_service(self.dir, autosave=False)
        self.scn = Scenario(self.svc)
        self.case = self.scn.submit()  # 挂起状态也能查看链路
        self.svc.resolve_conflict(self.case.case_id, self.scn.independent_reviewer,
                                  proceed=True, note="获利关系说明待补，先审内容")
        self.svc.review_case(self.case.case_id, self.scn.independent_reviewer,
                             DecisionType.REJECT, "多重违禁")

    def test_legal_sees_original_reviewer_sees_summary(self):
        legal_view = self.svc.case_view(self.case.case_id, self.scn.legal)
        self.assertEqual(legal_view["evidence_level"], "original")
        self.assertIn("evidence/video-ast-1.mp4", legal_view["asset"]["evidence_refs"])
        self.assertIn("evidence/license-li.jpg",
                      legal_view["asset"]["persona"]["evidence_refs"])

        reviewer_view = self.svc.case_view(self.case.case_id, self.scn.independent_reviewer)
        self.assertEqual(reviewer_view["evidence_level"], "summary")
        self.assertEqual(reviewer_view["asset"]["evidence_refs"], ["<原件仅限法务查看>"])
        self.assertEqual(reviewer_view["asset"]["persona"]["evidence_refs"],
                         ["<原件仅限法务查看>"])
        self.assertEqual(reviewer_view["asset"]["persona"]["authorizations"][0]["document_ref"],
                         "<授权书原件仅限法务查看>")
        # 摘要文字仍可见，审核工作不受影响
        self.assertIn("白大褂", reviewer_view["asset"]["summary"])

    def test_trace_reconstructs_full_chain(self):
        chain = self.svc.trace(self.case.case_id)
        self.assertEqual(chain["responsible_parties"]["beneficiary"], "辰光信息咨询有限公司")
        self.assertEqual(chain["responsible_parties"]["producer"], "幻影数字科技有限公司")
        self.assertEqual(chain["product"]["approval_no"], "国药准字Z00000001")
        latest = chain["decisions"][-1]
        self.assertEqual(latest["rule_pack_version"], "rules-2026.09")
        events = [e["event"] for e in chain["timeline"]]
        self.assertIn("案件提交", events)
        self.assertTrue(any(e.startswith("审核结论：reject") for e in events))
        # 规则命中随结论保存，可还原「谁在什么规则版本下做出判断」
        hit_ids = {h["rule_id"] for h in latest["hits"]}
        self.assertEqual(
            hit_ids,
            {"KW-CURE-001", "KW-ABS-001", "ID-MED-001", "AI-LBL-001"},
        )


if __name__ == "__main__":
    unittest.main()

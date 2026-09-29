"""审查工作流、分权、幂等、冲突、处置、时限恢复的端到端测试。"""

import tempfile
import unittest
from pathlib import Path

from src.synthetic_ad_review import store
from src.synthetic_ad_review.access import AccessError
from src.synthetic_ad_review.model import (
    AiLabel,
    CaseStatus,
    Decision,
    Evidence,
    IdentityAuthorization,
    ProductCategory,
    ReviewState,
    TaskKind,
    TaskStatus,
)
from src.synthetic_ad_review.reports import chain, render_text
from src.synthetic_ad_review.service import (
    ConflictError,
    RepostConflictError,
    ReviewService,
)
from tests._fixtures import (
    FakeClock,
    build_service,
    digital_person,
    full_ai_label,
    product,
    users,
)

CLEAN_GENERAL = "这是一款普通商品的客观介绍，按标签说明使用。"
HEALTH_V2 = ("经注册批准具有辅助降血糖的保健功能。本品不能代替药物。"
             "个体效果因人而异。本内容由 AI 生成。")


def make_case(svc, case_id="CASE-1", category=ProductCategory.GENERAL_GOODS,
              approval=None, beneficiary="P-BEN", queue="q1",
              asset_id="ASSET-1", white_coat=False, auth_id=None):
    return svc.create_case(
        case_id=case_id,
        title="测试广告",
        advertiser_party_id="P-ADV",
        beneficiary_party_id=beneficiary,
        product=product(category, approval, product_id=f"G-{case_id}"),
        digital_person=digital_person(asset_id, white_coat=white_coat,
                                      identity_auth_id=auth_id),
        ai_label=full_ai_label(),
        queue=queue,
    )


class ScriptVersionTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()

    def test_modified_script_creates_new_version_and_forces_rereview(self):
        case = make_case(self.svc)
        s1, reused = self.svc.submit_script(
            "CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.assertFalse(reused)
        self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE, reasons=["合规"])
        self.assertEqual(s1.review_state, ReviewState.APPROVED)

        # 已通过版本不能再审
        with self.assertRaisesRegex(ValueError, "已审核通过"):
            self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)

        # 脚本改动 → 新版本、新哈希、重新回到待审核
        s2, reused = self.svc.submit_script(
            "CASE-1", CLEAN_GENERAL + "新增一句描述。", "P-ADV", "短视频", "@acc1")
        self.assertFalse(reused)
        self.assertEqual(s2.version, 2)
        self.assertNotEqual(s1.hash, s2.hash)
        self.assertEqual(s2.review_state, ReviewState.PENDING)
        self.assertEqual(case.status, CaseStatus.PENDING_REVIEW)

    def test_duplicate_submission_reuses_prior_decision(self):
        c1 = make_case(self.svc, "CASE-1")
        self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE, reasons=["合规"])

        c2 = make_case(self.svc, "CASE-2")
        s2, reused = self.svc.submit_script(
            "CASE-2", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.assertTrue(reused)
        self.assertEqual(s2.review_state, ReviewState.APPROVED)
        self.assertEqual(c2.status, CaseStatus.APPROVED)
        opinion = c2.reviews[-1]
        self.assertEqual(opinion.decision, Decision.REUSE)
        self.assertIsNotNone(opinion.reused_from_review_id)
        self.assertEqual(opinion.rule_version, c1.reviews[-1].rule_version)

    def test_different_account_same_content_is_not_reuse(self):
        make_case(self.svc, "CASE-1")
        self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)
        make_case(self.svc, "CASE-2")
        _, reused = self.svc.submit_script(
            "CASE-2", CLEAN_GENERAL, "P-ADV", "短视频", "@acc2")
        self.assertFalse(reused)


class ConflictSuspensionTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()

    def test_missing_beneficiary_suspends_case(self):
        case = make_case(self.svc, beneficiary=None)
        with self.assertRaises(ConflictError):
            self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.assertEqual(case.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertIn("BENEFICIARY_UNKNOWN", [c.code for c in case.open_conflicts()])
        # 挂起期间不得通过
        with self.assertRaisesRegex(AccessError, "挂起调查"):
            self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)

        self.svc.resolve_conflict(
            self.u["op_q1"], "CASE-1", "BENEFICIARY_UNKNOWN",
            note="穿透结算确认获利方", beneficiary_party_id="P-BEN")
        self.assertEqual(case.status, CaseStatus.PENDING_REVIEW)
        self.assertEqual(case.beneficiary_party_id, "P-BEN")
        self.assertEqual([], case.open_conflicts())

    def test_approval_category_mismatch_suspends(self):
        case = make_case(self.svc, category=ProductCategory.DRUG,
                         approval="国食健字G20240188")
        with self.assertRaises(ConflictError):
            self.svc.submit_script("CASE-1", "请按说明书使用。", "P-ADV", "短视频", "@acc1")
        self.assertIn("APPROVAL_CATEGORY_MISMATCH",
                      [c.code for c in case.open_conflicts()])

    def test_general_goods_xiaoz_no_prefix_conflict(self):
        # 消字号文号不适用类别前缀规则，不应误报类别冲突
        case = make_case(self.svc, category=ProductCategory.GENERAL_GOODS,
                         approval="粤卫消证字(2025)第0317号")
        _, reused = self.svc.submit_script(
            "CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.assertFalse(reused)
        self.assertEqual(case.status, CaseStatus.PENDING_REVIEW)


class AccessControlTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()

    def test_queue_assignment_enforced(self):
        make_case(self.svc, queue="q1")
        self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        with self.assertRaisesRegex(AccessError, "未被分配到队列"):
            self.svc.review(self.u["op_q2"], "CASE-1", Decision.APPROVE)

    def test_interested_party_cannot_approve_own_ad(self):
        make_case(self.svc)
        self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        with self.assertRaisesRegex(AccessError, "利益关联"):
            self.svc.review(self.u["insider"], "CASE-1", Decision.APPROVE)

    def test_evidence_original_only_for_legal(self):
        case = make_case(self.svc)
        self.svc.add_evidence("CASE-1", Evidence(
            evidence_id="EV-1", case_id="CASE-1", kind="settlement",
            filename="s.xlsx", sha256="abc",
            summary="脱敏摘要：收款方为丁公司",
            original_uri="vault://legal/s.xlsx",
        ))
        legal = self.svc.get_evidence_view(self.u["legal"], "CASE-1", "EV-1")
        moderator = self.svc.get_evidence_view(self.u["mod_q1"], "CASE-1", "EV-1")
        self.assertEqual(legal["original_uri"], "vault://legal/s.xlsx")
        self.assertNotIn("vault://", moderator["original_uri"])
        self.assertEqual(moderator["summary"], "脱敏摘要：收款方为丁公司")
        self.assertEqual(case.evidence[0].summary, moderator["summary"])

    def test_task_assignee_only(self):
        self.svc.set_queue_assignee("q1", "op_q1")
        make_case(self.svc)
        self.svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)
        placement = self.svc.start_placement(
            self.u["op_q1"], "CASE-1", "短视频", "@acc1", "P-PUB")
        tasks = self.svc.takedown(self.u["op_q1"], "CASE-1", "复核风险")
        takedown = next(t for t in tasks if t.kind is TaskKind.TAKEDOWN)
        self.assertEqual(takedown.assignee_user, "op_q1")
        # 同队列但不是被分配人的运营者不能处理
        other_q1 = users()["mod_q1"]
        with self.assertRaisesRegex(AccessError, "无权处理"):
            self.svc.complete_task(other_q1, takedown.task_id)
        done = self.svc.complete_task(self.u["op_q1"], takedown.task_id, note="已删除")
        self.assertEqual(done.status, TaskStatus.DONE)
        self.assertFalse(placement.live)


class EnforcementTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()
        self.svc.set_queue_assignee("q1", "op_q1")

    def _approved_live_case(self, text=CLEAN_GENERAL):
        make_case(self.svc)
        self.svc.submit_script("CASE-1", text, "P-ADV", "短视频", "@acc1")
        self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)
        self.svc.start_placement(self.u["op_q1"], "CASE-1", "短视频", "@acc1", "P-PUB")

    def test_takedown_creates_notify_rectify_tasks(self):
        self._approved_live_case()
        tasks = self.svc.takedown(self.u["op_q1"], "CASE-1", "发现夸大用语")
        kinds = {t.kind for t in tasks}
        self.assertEqual(kinds, {TaskKind.TAKEDOWN, TaskKind.NOTIFY, TaskKind.RECTIFY})
        rectify = next(t for t in tasks if t.kind is TaskKind.RECTIFY)
        self.assertIsNotNone(rectify.due_at)

    def test_appeal_overturned_restores_placement(self):
        self._approved_live_case()
        tasks = self.svc.takedown(self.u["op_q1"], "CASE-1", "投诉待核")
        takedown = next(t for t in tasks if t.kind is TaskKind.TAKEDOWN)
        self.svc.complete_task(self.u["op_q1"], takedown.task_id)
        case = self.svc.cases["CASE-1"]
        self.assertFalse(case.placements[0].live)

        appeal = self.svc.file_appeal("CASE-1", "P-ADV", "内容合规，要求恢复")
        self.svc.decide_appeal(self.u["legal"], appeal.task_id, uphold=False,
                               note="投诉不实")
        self.assertEqual(case.status, CaseStatus.APPROVED)
        self.assertTrue(case.placements[0].live)
        self.assertTrue(all(
            t.status is TaskStatus.CANCELLED
            for t in tasks if t.kind is not TaskKind.TAKEDOWN or not t.completed_at
        ))

    def test_appeal_upheld_keeps_takedown(self):
        self._approved_live_case()
        tasks = self.svc.takedown(self.u["op_q1"], "CASE-1", "确认违规")
        appeal = self.svc.file_appeal("CASE-1", "P-ADV", "申诉")
        self.svc.decide_appeal(self.u["legal"], appeal.task_id, uphold=True,
                               note="违规成立")
        case = self.svc.cases["CASE-1"]
        self.assertEqual(case.status, CaseStatus.TAKEN_DOWN)
        open_notify = [t for t in tasks if t.kind is TaskKind.NOTIFY]
        self.assertTrue(open_notify[0].status is TaskStatus.OPEN)


class ReviewGuardsTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()

    def test_blocking_hit_cannot_approve(self):
        make_case(self.svc)
        self.svc.submit_script("CASE-1", "全网第一的好产品。", "P-ADV", "短视频", "@acc1")
        with self.assertRaisesRegex(AccessError, "不得通过"):
            self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)

    def test_restricted_claim_requires_substantiation(self):
        make_case(self.svc, category=ProductCategory.HEALTH_FOOD,
                  approval="国食健字G20240188")
        s, _ = self.svc.submit_script("CASE-1", HEALTH_V2, "P-ADV", "短视频", "@acc1")
        self.assertTrue(any(h.rule_id == "R-HEALTH-02" for h in s.hits))
        with self.assertRaisesRegex(AccessError, "证明文件"):
            self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)
        self.svc.add_evidence("CASE-1", Evidence(
            evidence_id="EV-1", case_id="CASE-1", kind="substantiation",
            filename="pijian.pdf", sha256="dd", summary="注册批件摘要",
            original_uri="vault://legal/pijian.pdf",
        ))
        opinion = self.svc.review(self.u["op_q1"], "CASE-1", Decision.APPROVE)
        self.assertEqual(opinion.decision, Decision.APPROVE)


class CrossAccountRepostTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = build_service()
        self.u = users()
        self.svc.set_queue_assignee("patrol", "patrol")
        self.svc.register_party(__import__("src.synthetic_ad_review.model",
                                           fromlist=["Party"]).Party(
            "P-PLATFORM", "平台", []))
        ointment = product(ProductCategory.GENERAL_GOODS,
                           "粤卫消证字(2025)第0317号", product_id="G-OINT")
        dp = digital_person("ASSET-DR", white_coat=True)
        self.svc.register_asset(dp, ointment, AiLabel(False, False), None)
        self.video_script = "白大褂出镜：这款药膏七天断根！"

    def test_patrol_registration_suspends_and_blocks_cross_account(self):
        with self.assertRaises(ConflictError):
            self.svc.register_live_placement(
                self.u["patrol"], "短视频", "@acc-a", "P-PUB",
                "ASSET-DR", self.video_script)
        c1 = self.svc.cases["CASE-PATROL-ASSET-DR-@acc-a"]
        self.assertEqual(c1.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertTrue(c1.placements[0].live)
        self.assertTrue(any(h.rule_id == "R-MEDCLAIM-01"
                            for h in c1.current_script().hits))

        # 获利方调查完成
        self.svc.resolve_conflict(
            self.u["patrol"], c1.case_id, "BENEFICIARY_UNKNOWN",
            note="已穿透", beneficiary_party_id="P-BEN")
        self.svc.review(self.u["patrol"], c1.case_id, Decision.REJECT,
                        reasons=["断根 + 无AI标识 + 无授权"])
        self.assertEqual(c1.status, CaseStatus.TAKEN_DOWN)

        # 同一素材换账号再发
        with self.assertRaises(RepostConflictError):
            self.svc.register_live_placement(
                self.u["patrol"], "短视频", "@acc-b", "P-PUB2",
                "ASSET-DR", self.video_script)
        c2 = self.svc.cases["CASE-PATROL-ASSET-DR-@acc-b"]
        self.assertEqual(c2.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertIn("REPOST_CROSS_ACCOUNT", [c.code for c in c2.open_conflicts()])
        # 原案件也留下被换号复用的痕迹
        self.assertTrue(any(e.action == "repost_detected" for e in c1.events))

    def test_start_placement_blocks_repost_after_takedown(self):
        # 正规案件通过、投放、下架后，另一主体用同素材、不同账号再送审
        self.svc.register_asset(
            digital_person("ASSET-2"),
            product(ProductCategory.GENERAL_GOODS, None, "G-2"),
            full_ai_label(), "P-BEN")
        c1 = self.svc.create_case(
            case_id="CASE-A", title="A", advertiser_party_id="P-ADV",
            beneficiary_party_id="P-BEN",
            product=product(ProductCategory.GENERAL_GOODS, None, "G-2"),
            digital_person=digital_person("ASSET-2"),
            ai_label=full_ai_label(), queue="q1")
        self.svc.submit_script("CASE-A", CLEAN_GENERAL, "P-ADV", "短视频", "@acc-a")
        self.svc.review(self.u["op_q1"], "CASE-A", Decision.APPROVE)
        self.svc.start_placement(self.u["op_q1"], "CASE-A", "短视频",
                                 "@acc-a", "P-PUB")
        self.svc.takedown(self.u["op_q1"], "CASE-A", "违规下架")

        c2 = self.svc.create_case(
            case_id="CASE-B", title="B", advertiser_party_id="P-ADV",
            beneficiary_party_id="P-BEN",
            product=product(ProductCategory.GENERAL_GOODS, None, "G-2"),
            digital_person=digital_person("ASSET-2"),
            ai_label=full_ai_label(), queue="q1")
        # 提交阶段即检出换账号再发并挂起，不进入审核
        with self.assertRaises(ConflictError):
            self.svc.submit_script("CASE-B", CLEAN_GENERAL, "P-ADV", "短视频", "@acc-b")
        self.assertEqual(c2.status, CaseStatus.UNDER_INVESTIGATION)
        self.assertIn("REPOST_CROSS_ACCOUNT",
                      [c.code for c in c2.open_conflicts()])


class DeadlineRecoveryTest(unittest.TestCase):
    def test_rectify_overdue_after_restart_escalates_once(self):
        clock = FakeClock()
        svc, _ = build_service(clock)
        svc.set_queue_assignee("q1", "op_q1")
        op = users()["op_q1"]
        make_case(svc)
        svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        svc.review(op, "CASE-1", Decision.APPROVE)
        svc.start_placement(op, "CASE-1", "短视频", "@acc1", "P-PUB")
        svc.takedown(op, "CASE-1", "违规")
        rectify = next(t for t in svc.cases["CASE-1"].tasks
                       if t.kind is TaskKind.RECTIFY)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            store.save(svc, path)
            # 服务停机：25 小时后新进程加载快照
            clock.advance(hours=25)
            restored = store.load(path, clock=clock)

        self.assertTrue(rectify.is_overdue(clock()))
        events = restored.advance_deadlines()
        case = restored.cases["CASE-1"]
        overdue_events = [e for e in events if e.action == "task_overdue"]
        self.assertTrue(overdue_events)
        self.assertTrue(any(e.action == "rectify_overdue_escalated" for e in events))
        # 系统强制执行：在投放置为下线，下架任务关闭
        self.assertFalse(any(p.live for p in case.placements))
        open_takedowns = [t for t in case.tasks
                          if t.kind is TaskKind.TAKEDOWN and t.status is TaskStatus.OPEN]
        self.assertEqual(open_takedowns, [])
        self.assertEqual(case.status, CaseStatus.TAKEN_DOWN)

        # 再次推进不重复处理（幂等）
        events2 = restored.advance_deadlines()
        self.assertEqual(events2, [])

    def test_completed_rectify_before_due_no_escalation(self):
        clock = FakeClock()
        svc, _ = build_service(clock)
        svc.set_queue_assignee("q1", "op_q1")
        op = users()["op_q1"]
        make_case(svc)
        svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        svc.review(op, "CASE-1", Decision.APPROVE)
        svc.start_placement(op, "CASE-1", "短视频", "@acc1", "P-PUB")
        tasks = svc.takedown(op, "CASE-1", "违规")
        rectify = next(t for t in tasks if t.kind is TaskKind.RECTIFY)
        svc.complete_task(op, rectify.task_id, note="已补标识并改版")
        clock.advance(hours=48)
        events = svc.advance_deadlines()
        self.assertEqual(events, [])


class IdentityAuthTest(unittest.TestCase):
    def test_valid_authorization_clears_auth_hit(self):
        svc, clock = build_service()
        auth = IdentityAuthorization(
            auth_id="AUTH-1", identity_owner_party_id="P-BEN",
            asset_id="ASSET-1", scope="保健食品科普", license_no="Y12345",
            valid_from="2026-01-01", valid_to="2026-12-31", on_file=True)
        svc.register_authorization(auth)
        case = make_case(svc, category=ProductCategory.HEALTH_FOOD,
                         approval="国食健字G20240188", white_coat=True,
                         auth_id="AUTH-1")
        s, _ = svc.submit_script("CASE-1", HEALTH_V2, "P-ADV", "短视频", "@acc1")
        # 有授权：无 R-AUTH-01（保健品允许医务人员形象，只要不构成具体疾病推荐场景之外的问题）
        self.assertFalse(any(h.rule_id == "R-AUTH-01" for h in s.hits))

    def test_expired_authorization_still_flags(self):
        svc, _ = build_service()
        auth = IdentityAuthorization(
            auth_id="AUTH-2", identity_owner_party_id="P-BEN",
            asset_id="ASSET-1", scope="科普", license_no=None,
            valid_from="2025-01-01", valid_to="2025-12-31", on_file=True)
        svc.register_authorization(auth)
        case = make_case(svc, white_coat=True, auth_id="AUTH-2")
        s, _ = svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        self.assertTrue(any(h.rule_id == "R-AUTH-01" for h in s.hits))


class ChainReportTest(unittest.TestCase):
    def test_chain_covers_full_lifecycle(self):
        svc, _ = build_service()
        u = users()
        make_case(svc)
        svc.submit_script("CASE-1", CLEAN_GENERAL, "P-ADV", "短视频", "@acc1")
        svc.review(u["op_q1"], "CASE-1", Decision.APPROVE, reasons=["合规"])
        svc.start_placement(u["op_q1"], "CASE-1", "短视频", "@acc1", "P-PUB")
        svc.takedown(u["op_q1"], "CASE-1", "发现风险")
        case = svc.cases["CASE-1"]
        data = chain(svc, case)

        for section in ("asset", "ai_label", "product", "parties", "scripts",
                        "placements", "tasks", "timeline"):
            self.assertIn(section, data)
        self.assertIn("P-STUDIO", data["asset"]["generator"])
        self.assertIn("P-BEN", data["parties"]["beneficiary"])
        self.assertEqual(data["scripts"][0]["review"]["decision"], "approve")
        self.assertEqual(data["placements"][0]["account"], "@acc1")
        self.assertTrue(any(t["kind"] == "下架" for t in data["tasks"]))

        text = render_text(svc, case)
        for heading in ("素材生成来源", "商品类别", "脚本版本与审核意见",
                        "责任主体与投放渠道", "处置结果", "完整时间线"):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()

"""白大褂数字人药膏广告的端到端演示。

运行：
    python3 -m src.synthetic_ad_review.demo

展示：登记 → 冲突挂起 → 调查放行 → 违禁驳回 → 修改重审通过 →
投放 → 撤销处置 → 队列执行 → 全链路还原，以及服务重启后时限推进。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

from types import SimpleNamespace

from .models import (
    AILabelStatus,
    Asset,
    AssetType,
    CaseStatus,
    DecisionType,
    IdentityAuthorization,
    Org,
    OrgRole,
    PersonaIdentity,
    Product,
    ProductCategory,
    Profession,
    User,
    UserRole,
)
from .service import ReviewService
from .store import Store

T0 = datetime(2026, 9, 27, 9, 0)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def build_world(svc: ReviewService):
    kangmin = svc.register_org(Org("org-kangmin", "康民制药有限公司", {OrgRole.MERCHANT}))
    huanying = svc.register_org(Org("org-huanying", "幻影数字科技有限公司", {OrgRole.PRODUCER}))
    huitong = svc.register_org(Org("org-huitong", "汇通电子商务有限公司", {OrgRole.PUBLISHER}))
    chenguang = svc.register_org(Org("org-chenguang", "辰光信息咨询有限公司", {OrgRole.BENEFICIARY}))
    platform = svc.register_org(Org("org-platform", "平台运营方", set()))

    reviewer = svc.register_user(User("u-rev", "周审核", platform.org_id, {UserRole.REVIEWER}))
    legal = svc.register_user(User("u-legal", "孙法务", platform.org_id, {UserRole.LEGAL}))
    operator = svc.register_user(User(
        "u-ops", "李运营", platform.org_id, {UserRole.OPERATOR}, {"ops:短视频"}))
    merchant_self = svc.register_user(User("u-self", "钱自检", kangmin.org_id, {UserRole.REVIEWER}))

    svc.register_identity(PersonaIdentity(
        "pid-li", "李医生", Profession.MEDICAL_STAFF, real_person=True,
        owner_org_id=huanying.org_id,
        evidence_refs=["证据原件/执业证书.jpg"],
        authorizations=[IdentityAuthorization(
            "auth-1", "李医生本人", huanying.org_id, "*",
            T0 - timedelta(days=30), T0 + timedelta(days=300), "证据原件/授权书.pdf")],
    ))
    svc.register_asset(Asset(
        "ast-1", AssetType.DIGITAL_HUMAN_VIDEO, huanying.org_id,
        generator_tool="幻影数字人引擎 v3",
        generated_at=T0 - timedelta(hours=2),
        ai_label=AILabelStatus.UNLABELED,
        content_hash="sha256:视频哈希",
        summary="穿白大褂的中年男性数字人在诊室口播药膏功效",
        persona_identity_id="pid-li",
        evidence_refs=["证据原件/视频母本.mp4"],
    ))
    svc.register_product(Product(
        "prd-gaoyao", "康民牌消痛膏药", ProductCategory.DRUG,
        registrant_org_id=kangmin.org_id,
        approval_no="国药准字Z00000001",
        link_org_id=huitong.org_id,  # 商品链接由汇通控制，与备案主体不一致
        link_url="https://shop.example/item/1",
    ))
    return SimpleNamespace(
        kangmin=kangmin, huanying=huanying, huitong=huitong, chenguang=chenguang,
        reviewer=reviewer, legal=legal, operator=operator, merchant_self=merchant_self,
    )


def main() -> None:
    clock = Clock()
    state_path = Path(tempfile.mkdtemp()) / "state.json"
    svc = ReviewService(Store(state_path), clock=clock)
    w = build_world(svc)

    print("=" * 70)
    print("1. 提交：白大褂数字人、脚本/链接/账户分属三家公司、另有获利方")
    print("=" * 70)
    case = svc.submit_case(
        title="白大褂数字人口播药膏",
        asset_id="ast-1",
        product_id="prd-gaoyao",
        merchant_org_id="org-kangmin",
        beneficiary_org_id="org-chenguang",
        script_content="这款康民牌消痛药膏可以断根，永不复发，李医生推荐大家购买！",
        channel="短视频",
        account_org_id="org-huitong",
        account_name="汇通旗舰店",
        author_org_id="org-huanying",
    )
    print(f"案件 {case.case_id} 状态：{case.status.value}")
    for note in case.conflict_notes:
        print(f"  · 冲突挂起：{note}")

    print("\n" + "=" * 70)
    print("2. 调查后放行内容审核；规则引擎命中阻断项，驳回")
    print("=" * 70)
    svc.resolve_conflict(case.case_id, w.reviewer, proceed=True,
                         note="汇通与康民为代运营关系；获利关系继续核查")
    decision = svc.review_case(
        case.case_id, w.reviewer, DecisionType.REJECT,
        "含断根等违禁功效保证、医务人员代言药品、无AI标识")
    for hit in decision.hits:
        print(f"  · [{hit.severity.value}] {hit.rule_id} {hit.message} 命中：{hit.matched_terms}")
    print(f"规则包版本：{decision.rule_pack_version}；案件状态：{case.status.value}")

    print("\n" + "=" * 70)
    print("3. 合规改写脚本后强制重审；商品方试图自批 → 被拒绝；独立审核员通过")
    print("=" * 70)
    clock.now += timedelta(hours=1)
    svc.revise_script(
        case.case_id,
        "康民牌消痛膏药，请按药品说明书或在药师指导下购买和使用。",
        author_org_id="org-huanying",
    )
    print(f"  · 脚本修改后状态回到：{case.status.value}（须重新审核）")
    # 演示素材重新出片：移除医务人员形象（药品广告禁用代言）并补齐AI标识
    svc.assets["ast-1"].persona_identity_id = None
    svc.assets["ast-1"].ai_label = AILabelStatus.LABELED
    try:
        svc.review_case(case.case_id, w.merchant_self, DecisionType.APPROVE, "自批")
    except Exception as exc:
        print(f"  · 自批被拦截：{exc}")
    svc.review_case(case.case_id, w.reviewer, DecisionType.APPROVE, "合规版本通过")
    print(f"  · 复审通过，案件状态：{case.status.value}")

    print("\n" + "=" * 70)
    print("4. 投放 → 事后巡检撤销 → 生成下架/通知/整改任务")
    print("=" * 70)
    placement = svc.place(case.case_id, channel="短视频",
                          account_org_id="org-huitong", account_name="汇通旗舰店")
    print(f"  · 投放 {placement.placement_id}，固定脚本版本 {placement.script_version_id}")
    clock.now += timedelta(hours=2)
    svc.revoke(case.case_id, w.reviewer, reason="巡检发现授权范围存疑")
    for t in svc.queue_tasks(w.operator):
        print(f"  · 任务 {t.task_id} {t.task_type.value}，队列 {t.assignee_queue}，"
              f"截止 {t.deadline:%m-%d %H:%M}")

    print("\n" + "=" * 70)
    print("5. 服务停机 26 小时后恢复：时限按绝对时间推进，任务自动逾期")
    print("=" * 70)
    clock.now += timedelta(hours=26)
    svc2 = ReviewService(Store(state_path), clock=clock)
    for t in svc2.queue_tasks(w.operator):
        print(f"  · {t.task_type.value}：{t.status.value}（截止 {t.deadline:%m-%d %H:%M}）")

    print("\n" + "=" * 70)
    print("6. 队列运营者完成下架与通知，案件进入结案")
    print("=" * 70)
    for t in svc2.queue_tasks(w.operator):
        svc2.complete_task(t.task_id, w.operator, "已执行")
        print(f"  · {t.task_type.value} 完成")
    print(f"  · 案件状态：{svc2.cases[case.case_id].status.value}")

    print("\n" + "=" * 70)
    print("7. 全链路还原（法务视角含证据原件；普通审核员只见摘要）")
    print("=" * 70)
    chain = svc2.case_view(case.case_id, w.legal)
    print("责任主体：", json.dumps(chain["responsible_parties"], ensure_ascii=False, indent=2))
    print("时间线：")
    for e in chain["timeline"]:
        stamp = e["at"][5:16].replace("T", " ")
        print(f"  {stamp}  {e['event']}  [{e['ref']}]")
    reviewer_view = svc2.case_view(case.case_id, w.reviewer)
    print("普通审核员可见的素材证据：", reviewer_view["asset"]["evidence_refs"])


if __name__ == "__main__":
    main()

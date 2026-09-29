"""端到端演示：数字人健康广告审查服务。

用法：
    python3 -m src.synthetic_ad_review.cli

场景一（巡查发现的白大褂药膏视频）：
    无 AI 标识、无医师授权、宣称“断根”、脚本/链接/账户分属三家、获利方不明
    → 挂起调查 → 补获利方 → 驳回在投版本 → 下架/通知/整改 → 换账号再发被拦截
    → 快照落盘 → 模拟停机 25 小时后恢复，整改逾期自动升级。

场景二（保健食品正规送审）：
    v1 含疾病治疗用语被驳回 → v2 修改后重审（限制性声称需凭证）
    → 利益关联方试图自批被拦截 → 通过 → 投放 → 下架 → 申诉成立恢复
    → 同素材同脚本同账号重复提交沿用原结论。
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from . import store
from .access import AccessError, User
from .model import (
    AiLabel,
    DigitalPerson,
    Evidence,
    Party,
    PartyRole,
    Product,
    ProductCategory,
    ReviewerRole,
    Decision,
)
from .reports import render_text
from .service import ConflictError, RepostConflictError, ReviewService

SEP = "=" * 78


class Clock:
    """可拨快的时钟，用于演示服务停机后恢复对整改时限的影响。"""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def main() -> None:
    from .model import utc_now

    t0 = utc_now().replace(microsecond=0)
    clock = Clock(t0)
    svc = ReviewService(clock=clock)

    # ---------------- 主体 ----------------
    p_script = Party("P-SCRIPT", "星语脚本文化传播有限公司",
                     [PartyRole.ADVERTISER], "91440101MA01SCRIPT")
    p_link = Party("P-LINK", "康泰医药电商有限公司",
                   [], "91440101MA02LINK0")
    p_pub = Party("P-PUB", "迅达信息流科技有限公司",
                  [PartyRole.PUBLISHER], "91440101MA03PUB000")
    p_pub2 = Party("P-PUB2", "黑马矩阵营销中心",
                   [PartyRole.PUBLISHER], "91440101MA04PUB200")
    p_studio = Party("P-STUDIO", "幻镜数字人科技有限公司",
                     [PartyRole.PRODUCER], "91440101MA05STUDIO")
    p_beneficiary = Party("P-BENEFICIARY", "佰利康健康管理有限公司",
                          [PartyRole.BENEFICIARY], "91440101MA06BENEF0")
    p_health = Party("P-HEALTH", "益膳保健食品股份有限公司",
                     [PartyRole.ADVERTISER, PartyRole.BENEFICIARY],
                     "91440101MA07HEALTH")
    for p in (p_script, p_link, p_pub, p_pub2, p_studio, p_beneficiary, p_health):
        svc.register_party(p)

    # 岗位用户：队列分配 + 利益关联
    wang = User("wang", ReviewerRole.OPERATOR, queues=("patrol", "health"))
    zhao = User("zhao", ReviewerRole.OPERATOR, queues=("beauty",))
    sun = User("sun", ReviewerRole.LEGAL, queues=("health", "patrol"))
    fang_agent = User("fang_agent", ReviewerRole.OPERATOR,
                      queues=("health",), affiliated_party_ids=("P-HEALTH",))
    svc.set_queue_assignee("patrol", "wang")
    svc.set_queue_assignee("health", "wang")

    print(SEP)
    print("场景一：巡查发现白大褂数字人在投药膏视频（无 AI 标识 / 宣称断根）")
    print(SEP)

    ointment = Product(
        product_id="G-FLQ-01", name="肤立清抑菌膏",
        category=ProductCategory.GENERAL_GOODS,
        approval_no="粤卫消证字(2025)第0317号",
        link_owner_party_id="P-LINK",
    )
    dp_a = DigitalPerson(
        asset_id="ASSET-DR-LI", name="李医生形象数字人",
        generator_party_id="P-STUDIO", model_provider="幻镜Avatar-3",
        white_coat=True, identity_auth_id=None,
        source_prompt_ref="oss://prompts/ASSET-DR-LI/prompt_v9.json",
        generation_log_ref="oss://genlogs/ASSET-DR-LI/",
    )
    # 素材台账先行；获利方线索缺失
    svc.register_asset(dp_a, ointment, AiLabel(False, False), beneficiary_party_id=None)

    script_a = (
        "【白大褂数字人出镜】大家好，我是皮肤科李医生。这款肤立清抑菌膏我亲测有效，"
        "抹上七天皮炎断根、药到病除，永不复发，安全无副作用，全网第一，赶紧下单！"
    )

    # 巡查立案：视频已在投，脚本由 P-SCRIPT 撰写、链接指向 P-LINK、账号属于 P-PUB
    try:
        case_a, _ = svc.register_live_placement(
            wang, channel="短视频平台", publisher_account="@xunda_tui",
            publisher_party_id="P-PUB", asset_id="ASSET-DR-LI",
            script_text=script_a,
        )
    except ConflictError as exc:
        case_a = svc.cases["CASE-PATROL-ASSET-DR-LI-@xunda_tui"]
        print(f"提交即挂起：{exc}")
    # 调查依据脚本合同与分账记录更正广告主（脚本撰写方），操作本身记入时间线
    svc.update_advertiser(wang, case_a.case_id, "P-SCRIPT")
    print(f"案件 {case_a.case_id} 状态：{case_a.status.label}")
    print("当前脚本命中：")
    for h in case_a.current_script().hits:
        print(f"  - {h.rule_id}（{h.severity.value}）{h.term}：{h.reason}")

    # 证据存证
    svc.add_evidence(case_a.case_id, Evidence(
        evidence_id="EV-VIDEO-1", case_id=case_a.case_id, kind="video",
        filename="drli_ointment.mp4",
        sha256="9f2c1a...e7",
        summary="白大褂数字人推荐药膏，口播含“断根”等表述，画面与文件均无 AI 生成标识",
        original_uri="vault://legal/CASE-DRLI/drli_ointment.mp4",
    ))
    svc.add_evidence(case_a.case_id, Evidence(
        evidence_id="EV-SETTLE-1", case_id=case_a.case_id, kind="settlement",
        filename="settlement_2026W39.xlsx",
        sha256="41be02...5a",
        summary="近30日结算流水显示实际收款方为佰利康健康管理有限公司（账户信息已脱敏）",
        original_uri="vault://legal/CASE-DRLI/settlement_2026W39.xlsx",
        submitted_by_party_id="P-PLATFORM",
    ))

    print()
    print("证据分权查看同一份结算流水：")
    legal_view = svc.get_evidence_view(sun, case_a.case_id, "EV-SETTLE-1")
    mod_view = svc.get_evidence_view(wang, case_a.case_id, "EV-SETTLE-1")
    print(f"  法务 {sun.username}：{legal_view['original_uri']}（{legal_view['note']}）")
    print(f"  审核员 {wang.username}：{mod_view['original_uri']}（{mod_view['note']}）")

    # 调查补登记获利方，解除挂起
    svc.resolve_conflict(
        wang, case_a.case_id, "BENEFICIARY_UNKNOWN",
        note="结算流水穿透确认收款主体为佰利康公司，已补登记并纳入责任链",
        beneficiary_party_id="P-BENEFICIARY",
    )
    print(f"冲突解除后案件状态：{case_a.status.label}")

    # 审核驳回在投版本 → 自动生成下架/通知/整改任务
    opinion = svc.review(
        wang, case_a.case_id, Decision.REJECT,
        reasons=["非药品使用“断根”等医疗用语，构成虚假宣传",
                 "白大褂数字人无身份授权，且未标注 AI 生成标识",
                 "实际获利方与广告主、发布者分离，已穿透记录"],
    )
    print(f"审核结论 {opinion.review_id}：{opinion.decision.value}（规则 {opinion.rule_version}）")
    print("自动生成处置任务：")
    for task in case_a.tasks:
        print(f"  - [{task.kind.label}] {task.task_id} 处理人={task.assignee_user}"
              f" 截止={task.due_at or '—'}")

    # 同一素材换账号再发 → 拦截
    print()
    try:
        svc.register_live_placement(
            wang, channel="短视频平台", publisher_account="@yisheng_tui",
            publisher_party_id="P-PUB2", asset_id="ASSET-DR-LI",
            script_text=script_a,
        )
    except RepostConflictError as exc:
        print(f"换账号再发被拦截：{exc}")
    repost_case = svc.cases["CASE-PATROL-ASSET-DR-LI-@yisheng_tui"]
    print(f"新案件状态：{repost_case.status.label}，冲突："
          f"{[c.code for c in repost_case.open_conflicts()]}")

    # 跨队列运营者无权处理
    print()
    try:
        svc.complete_task(zhao, case_a.tasks[0].task_id)
    except AccessError as exc:
        print(f"分权拦截：{exc}")

    # 本队列运营者执行下架
    takedown_task = next(t for t in case_a.tasks if t.kind.value == "takedown")
    svc.complete_task(wang, takedown_task.task_id, note="视频已删除，素材加入在投拦截库")
    print(f"下架执行后在投状态：{[p.live for p in case_a.placements]}")

    print()
    print(render_text(svc, case_a))

    # 快照落盘，模拟服务停机
    snap_dir = Path(tempfile.mkdtemp(prefix="adreview-"))
    snap_path = snap_dir / "snapshot.json"
    store.save(svc, snap_path)
    print()
    print(f"服务快照已落盘：{snap_path}")

    print()
    print(SEP)
    print("模拟服务停机 25 小时后恢复（整改时限为绝对时间，逾期自动升级）")
    print(SEP)
    clock.t = t0 + timedelta(hours=25)
    svc2 = store.load(snap_path, clock=clock)
    events = svc2.advance_deadlines()
    case_a2 = svc2.cases[case_a.case_id]
    for e in events:
        print(f"  - {e.action}：{e.detail}")
    if any(e.action == "rectify_overdue_escalated" for e in events):
        print("  整改逾期 → 系统强制执行下架，在投版本全部下线，整改任务关闭")
    print(f"  当前在投状态：{[p.live for p in case_a2.placements]}")

    print()
    print(SEP)
    print("场景二：保健食品送审——改版重审、回避、凭证、投放、申诉与幂等")
    print(SEP)

    protein = Product(
        product_id="H-YST-88", name="益膳堂蛋白粉",
        category=ProductCategory.HEALTH_FOOD,
        approval_no="国食健字G20240188",
        registrant_party_id="P-HEALTH",
        link_owner_party_id="P-HEALTH",
        indications="辅助降血糖",
    )
    dp_b = DigitalPerson(
        asset_id="ASSET-YST", name="益膳堂营养师数字人",
        generator_party_id="P-STUDIO", model_provider="幻镜Avatar-3",
        white_coat=False,
        source_prompt_ref="oss://prompts/ASSET-YST/prompt_v2.json",
        generation_log_ref="oss://genlogs/ASSET-YST/",
    )
    label_ok = AiLabel(True, True, label_text="本内容由 AI 生成")
    case_b = svc2.create_case(
        case_id="CASE-YST-001", title="益膳堂蛋白粉信息流广告",
        advertiser_party_id="P-HEALTH", product=protein,
        digital_person=dp_b, ai_label=label_ok, queue="health",
        beneficiary_party_id="P-HEALTH",
    )

    v1 = ("益膳堂蛋白粉，喝了三周血糖就正常，对糖尿病有治疗效果，能替代药物，全网第一！")
    s1, reused = svc2.submit_script(case_b.case_id, v1, "P-HEALTH",
                                    channel="直播间", publisher_account="@yishan-live")
    print(f"v1 提交（沿用结论={reused}），命中：")
    for h in s1.hits:
        print(f"  - {h.rule_id}（{h.severity.value}）{h.term}")
    svc2.review(wang, case_b.case_id, Decision.REJECT,
                reasons=["保健食品不得涉及疾病治疗功能、不得声称替代药物",
                         "脚本缺少“本品不能代替药物”提示语", "含绝对化用语"])
    print(f"v1 结论：{case_b.status.label}")

    v2 = ("益膳堂蛋白粉，经注册批准具有辅助降血糖的保健功能，适合血糖偏高人群日常补充。"
          "本品不能代替药物。个体效果因人而异，请按标签说明食用。本内容由 AI 生成。")
    s2, reused = svc2.submit_script(case_b.case_id, v2, "P-HEALTH",
                                    channel="直播间", publisher_account="@yishan-live")
    print(f"脚本修改后生成 v2（哈希变化={'是' if s1.hash != s2.hash else '否'}），剩余命中：")
    for h in s2.hits:
        print(f"  - {h.rule_id}（{h.severity.value}）{h.term}")

    # 利益关联方试图批准自己的广告
    try:
        svc2.review(fang_agent, case_b.case_id, Decision.APPROVE)
    except AccessError as exc:
        print(f"回避拦截：{exc}")

    # 限制性声称需凭证
    try:
        svc2.review(wang, case_b.case_id, Decision.APPROVE, reasons=["形式合规"])
    except AccessError as exc:
        print(f"凭证拦截：{exc}")
    svc2.add_evidence(case_b.case_id, Evidence(
        evidence_id="EV-SUB-1", case_id=case_b.case_id, kind="substantiation",
        filename="jianhao_G20240188.pdf", sha256="77ab91...0c",
        summary="保健食品注册批件（辅助降血糖）及功能学评价报告摘要",
        original_uri="vault://legal/CASE-YST/jianhao_G20240188.pdf",
        submitted_by_party_id="P-HEALTH",
    ))
    svc2.review(wang, case_b.case_id, Decision.APPROVE,
                reasons=["脚本与批件功能声称一致，提示语齐备，限制性声称有凭证"])
    print(f"v2 结论：{case_b.status.label}")

    placement = svc2.start_placement(
        wang, case_b.case_id, channel="直播间",
        publisher_account="@yishan-live", publisher_party_id="P-HEALTH",
    )
    print(f"投放登记：{placement.placement_id} @ {placement.channel} 采用脚本 v{placement.script_version}")

    # 重复提交：同素材 + 同脚本哈希 + 同账号 → 沿用原结论
    case_b2 = svc2.create_case(
        case_id="CASE-YST-002", title="益膳堂蛋白粉信息流广告（重复送审）",
        advertiser_party_id="P-HEALTH", product=protein,
        digital_person=dp_b, ai_label=label_ok, queue="health",
        beneficiary_party_id="P-HEALTH",
    )
    _, reused = svc2.submit_script(case_b2.case_id, v2, "P-HEALTH",
                                   channel="直播间", publisher_account="@yishan-live")
    reuse_review = case_b2.reviews[-1]
    print(f"重复提交：沿用结论={reused}，系统意见 {reuse_review.review_id} "
          f"指向原审核 {reuse_review.reused_from_review_id}，案件状态={case_b2.status.label}")

    # 事后风险处置 → 通知/下架/整改 → 申诉成立恢复
    tasks = svc2.takedown(wang, case_b.case_id,
                          reason="接到投诉称直播口径存在暗示治疗作用，暂停投放复核")
    appeal = svc2.file_appeal(case_b.case_id, "P-HEALTH",
                              "送审脚本与实际播出口播一致，无疾病治疗表述，附批件为证")
    print(f"处置生成 {len(tasks)} 个任务；申诉任务 {appeal.task_id} 已受理")
    svc2.decide_appeal(sun, appeal.task_id, uphold=False,
                       note="复核直播录像与 v2 脚本一致，不存在疾病治疗表述，申诉成立")
    print(f"申诉裁决后案件状态：{case_b.status.label}；"
          f"在投恢复：{[p.live for p in case_b.placements]}；"
          f"原处置任务状态：{[t.status.value for t in tasks]}")

    print()
    print(render_text(svc2, case_b))


if __name__ == "__main__":
    main()

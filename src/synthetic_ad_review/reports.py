"""案件链路还原。

最终一条广告要能还原：
素材（谁生成、用谁的身份）→ 脚本（各版本、撰写方、风险命中、审核意见）
→ 商品（类别、文号、注册人、链接方）→ 责任主体（广告主/发布者/获利方）
→ 投放渠道与账号 → 处置结果（下架、通知、整改、申诉）→ 时间线事件。
"""

from __future__ import annotations

from typing import Any

from .model import Case, ReviewerRole, TaskStatus


def _party_name(service, party_id: str | None) -> str:
    if not party_id:
        return "未登记"
    party = service.parties.get(party_id)
    return f"{party.name}（{party_id}）" if party else f"未知主体（{party_id}）"


def chain(service, case: Case) -> dict[str, Any]:
    """结构化链路：供程序消费与归档。"""
    dp = case.digital_person
    auth = service.authorizations.get(dp.identity_auth_id) if dp.identity_auth_id else None
    current = case.current_script()

    return {
        "case_id": case.case_id,
        "title": case.title,
        "status": case.status.label,
        "queue": case.queue,
        "asset": {
            "asset_id": dp.asset_id,
            "name": dp.name,
            "white_coat": dp.white_coat,
            "generator": _party_name(service, dp.generator_party_id),
            "model_provider": dp.model_provider,
            "source_prompt_ref": dp.source_prompt_ref,
            "generation_log_ref": dp.generation_log_ref,
            "identity_authorization": None if auth is None else {
                "auth_id": auth.auth_id,
                "owner": _party_name(service, auth.identity_owner_party_id),
                "scope": auth.scope,
                "license_no": auth.license_no,
                "valid": f"{auth.valid_from} ~ {auth.valid_to}",
                "on_file": auth.on_file,
            },
        },
        "ai_label": {
            "conspicuous": case.ai_label.conspicuous_label,
            "implicit_watermark": case.ai_label.implicit_watermark,
            "complete": case.ai_label.complete,
        },
        "product": {
            "product_id": case.product.product_id,
            "name": case.product.name,
            "category": case.product.category.label,
            "approval_no": case.product.approval_no,
            "registrant": _party_name(service, case.product.registrant_party_id),
            "link_owner": _party_name(service, case.product.link_owner_party_id),
        },
        "parties": {
            "advertiser": _party_name(service, case.advertiser_party_id),
            "publisher_accounts": sorted({
                f"{p.publisher_account} / {_party_name(service, p.publisher_party_id)}"
                for p in case.placements
            }),
            "beneficiary": _party_name(service, case.beneficiary_party_id),
        },
        "scripts": [
            {
                "version": s.version,
                "hash": s.hash,
                "author": _party_name(service, s.author_party_id),
                "state": s.review_state.value,
                "hits": [
                    {"rule": h.rule_id, "severity": h.severity.value,
                     "term": h.term, "excerpt": h.evidence_excerpt}
                    for h in s.hits
                ],
                "review": None if not s.review_id else {
                    "review_id": s.review_id,
                    "reviewer": _reviewer_label(case, s.review_id),
                    "decision": case.review_for(s.version).decision.value,
                    "reasons": case.review_for(s.version).reasons,
                    "rule_version": case.review_for(s.version).rule_version,
                },
            }
            for s in case.scripts
        ],
        "placements": [
            {
                "placement_id": p.placement_id,
                "channel": p.channel,
                "account": p.publisher_account,
                "publisher": _party_name(service, p.publisher_party_id),
                "script_version": p.script_version,
                "live": p.live,
                "started_at": p.started_at,
                "ended_at": p.ended_at,
            }
            for p in case.placements
        ],
        "conflicts": [
            {"code": c.code, "message": c.message,
             "resolved": c.resolved, "resolution": c.resolution_note}
            for c in case.conflicts
        ],
        "tasks": [
            {"task_id": t.task_id, "kind": t.kind.label, "assignee": t.assignee_user,
             "status": t.status.value, "due_at": t.due_at, "note": t.note}
            for t in case.tasks
        ],
        "current_version": current.version if current else None,
        "timeline": [
            {"seq": e.seq, "at": e.at, "actor": e.actor,
             "action": e.action, "detail": e.detail}
            for e in case.events
        ],
    }


def _reviewer_label(case: Case, review_id: str) -> str:
    for r in case.reviews:
        if r.review_id == review_id:
            if r.reviewer_role is ReviewerRole.SYSTEM:
                return "系统（沿用原结论）"
            return f"{r.reviewer_user}（{r.reviewer_role.label}）"
    return "未知"


def render_text(service, case: Case) -> str:
    """人类可读的链路报告。"""
    c = chain(service, case)
    lines: list[str] = []
    add = lines.append

    add(f"案件 {c['case_id']}｜{c['title']}")
    add(f"状态：{c['status']}　队列：{c['queue']}")
    add("")
    add("【一、素材生成来源】")
    asset = c["asset"]
    add(f"  素材 {asset['asset_id']}《{asset['name']}》 白大褂形象：{'是' if asset['white_coat'] else '否'}")
    add(f"  生成方：{asset['generator']}　模型：{asset['model_provider']}")
    add(f"  提示词存证：{asset['source_prompt_ref']}　生成日志：{asset['generation_log_ref']}")
    if asset["identity_authorization"]:
        a = asset["identity_authorization"]
        add(f"  身份授权：{a['auth_id']}　权利人：{a['owner']}　范围：{a['scope']}")
        add(f"    资质号：{a['license_no']}　有效期：{a['valid']}　已存证：{a['on_file']}")
    elif asset["white_coat"]:
        add("  身份授权：无（医务身份素材来源不可追溯）")
    else:
        add("  身份授权：非医务身份形象，无需身份授权")
    label = c["ai_label"]
    add(f"  AI 标识：显著={label['conspicuous']} 隐式水印={label['implicit_watermark']} "
        f"齐备={label['complete']}")

    add("")
    add("【二、商品类别】")
    p = c["product"]
    add(f"  {p['name']}（{p['product_id']}）类别：{p['category']}　文号：{p['approval_no'] or '无'}")
    add(f"  注册/备案主体：{p['registrant']}　商品链接控制方：{p['link_owner']}")

    add("")
    add("【三、脚本版本与审核意见】")
    for s in c["scripts"]:
        add(f"  v{s['version']} [{s['hash']}] 撰写方：{s['author']} 状态：{s['state']}")
        for h in s["hits"]:
            add(f"    - 命中 {h['rule']}（{h['severity']}）{h['term']}：{h['excerpt']}")
        if s["review"]:
            rv = s["review"]
            add(f"    审核 {rv['review_id']}｜{rv['reviewer']}｜结论 {rv['decision']}"
                f"｜规则版本 {rv['rule_version']}")
            for reason in rv["reasons"]:
                add(f"      · {reason}")

    add("")
    add("【四、责任主体与投放渠道】")
    add(f"  广告主：{c['parties']['advertiser']}")
    add(f"  实际获利方：{c['parties']['beneficiary']}")
    for acct in c["parties"]["publisher_accounts"]:
        add(f"  投放账号：{acct}")

    add("")
    add("【五、处置结果】")
    if not c["tasks"]:
        add("  暂无处置任务")
    for t in c["tasks"]:
        overdue = ""
        if t["status"] == TaskStatus.OPEN.value and t["due_at"]:
            overdue = f"　截止 {t['due_at']}"
        add(f"  [{t['kind']}] {t['task_id']} 处理人：{t['assignee'] or '未分配'}"
            f" 状态：{t['status']}{overdue}")
        if t["note"]:
            add(f"      {t['note']}")
    for conflict in c["conflicts"]:
        flag = "已解除" if conflict["resolved"] else "未决"
        add(f"  [挂起·{flag}] {conflict['code']}：{conflict['message']}")

    add("")
    add("【六、完整时间线】")
    for e in c["timeline"]:
        detail = "　".join(f"{k}={v}" for k, v in e["detail"].items()
                           if k in ("version", "conflict", "decision", "task_id",
                                    "channel", "account", "reason", "note",
                                    "reused_from", "by_case", "existing_case"))
        add(f"  {e['seq']:>2}. {e['at']} {e['actor']} → {e['action']}"
            + (f"　{detail}" if detail else ""))
    return "\n".join(lines)

"""快照持久化：整个服务落盘为 JSON，可在新进程中恢复。

整改/处置的截止时间是落盘的绝对时间（due_at），恢复后由 advance_deadlines
继续推进，不依赖任何内存定时器或进程存活时间。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from .model import (
    AiLabel,
    Case,
    Conflict,
    DigitalPerson,
    Evidence,
    Event,
    IdentityAuthorization,
    Party,
    PlacementSnapshot,
    Product,
    ProductCategory,
    PartyRole,
    ReviewOpinion,
    ReviewerRole,
    ReviewState,
    RiskHit,
    ScriptVersion,
    Severity,
    CaseStatus,
    Decision,
    Task,
    TaskKind,
    TaskStatus,
)
from . import rules as rules_module
from .service import ReviewService

SNAPSHOT_VERSION = 1


def _enum(cls, value):
    return cls(value) if value is not None else None


def save(service: ReviewService, path: str | Path) -> None:
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "rulebook_version": rules_module.RULEBOOK_VERSION,
        "parties": [p.__dict__ for p in service.parties.values()],
        "authorizations_full": [a.__dict__ for a in service.authorizations.values()],
        "queue_assignees": service.queue_assignees,
        "decision_index": service.decision_index,
        "asset_registry": [
            {
                "asset_id": asset_id,
                "digital_person": dp.__dict__,
                "product": service._asset_products[asset_id].__dict__,
                "ai_label": service._asset_labels[asset_id].__dict__,
                "beneficiary_party_id": service._asset_beneficiaries.get(asset_id),
            }
            for asset_id, dp in service._assets.items()
        ],
        "cases": [c.to_dict() for c in service.cases.values()],
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _json_default(obj):
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"不可序列化的对象：{type(obj)!r}")


def load(path: str | Path, clock=None) -> ReviewService:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    service = ReviewService(clock=clock) if clock is not None else ReviewService()

    for raw in payload["parties"]:
        service.register_party(Party(
            party_id=raw["party_id"],
            name=raw["name"],
            roles=[PartyRole(r) for r in raw["roles"]],
            credit_code=raw.get("credit_code"),
        ))

    # 授权需要独立保存；为简化快照结构，授权存在案件数字人的关联信息之外单独存放
    for raw in payload.get("authorizations_full", []):
        service.register_authorization(_authorization(raw))

    service.queue_assignees = dict(payload.get("queue_assignees", {}))
    service.decision_index = dict(payload.get("decision_index", {}))

    for raw in payload.get("asset_registry", []):
        dp_raw = raw["digital_person"]
        digital_person = DigitalPerson(
            asset_id=dp_raw["asset_id"], name=dp_raw["name"],
            generator_party_id=dp_raw["generator_party_id"],
            model_provider=dp_raw["model_provider"],
            white_coat=dp_raw.get("white_coat", False),
            identity_auth_id=dp_raw.get("identity_auth_id"),
            source_prompt_ref=dp_raw.get("source_prompt_ref"),
            generation_log_ref=dp_raw.get("generation_log_ref"),
        )
        prod_raw = raw["product"]
        product = Product(
            product_id=prod_raw["product_id"], name=prod_raw["name"],
            category=ProductCategory(prod_raw["category"]),
            approval_no=prod_raw.get("approval_no"),
            registrant_party_id=prod_raw.get("registrant_party_id"),
            link_owner_party_id=prod_raw.get("link_owner_party_id"),
            indications=prod_raw.get("indications"),
            required_warnings=list(prod_raw.get("required_warnings", [])),
        )
        label_raw = raw["ai_label"]
        ai_label = AiLabel(
            conspicuous_label=label_raw.get("conspicuous_label", False),
            implicit_watermark=label_raw.get("implicit_watermark", False),
            label_text=label_raw.get("label_text"),
        )
        service.register_asset(
            digital_person, product, ai_label,
            beneficiary_party_id=raw.get("beneficiary_party_id"),
        )

    for raw in payload["cases"]:
        case = _case(raw)
        service.cases[case.case_id] = case

    return service


def _authorization(raw: dict) -> IdentityAuthorization:
    return IdentityAuthorization(
        auth_id=raw["auth_id"],
        identity_owner_party_id=raw["identity_owner_party_id"],
        asset_id=raw["asset_id"],
        scope=raw["scope"],
        license_no=raw.get("license_no"),
        valid_from=raw["valid_from"],
        valid_to=raw["valid_to"],
        on_file=raw.get("on_file", True),
    )


def _case(raw: dict) -> Case:
    product_raw = raw["product"]
    product = Product(
        product_id=product_raw["product_id"],
        name=product_raw["name"],
        category=ProductCategory(product_raw["category"]),
        approval_no=product_raw.get("approval_no"),
        registrant_party_id=product_raw.get("registrant_party_id"),
        link_owner_party_id=product_raw.get("link_owner_party_id"),
        indications=product_raw.get("indications"),
        required_warnings=list(product_raw.get("required_warnings", [])),
    )
    dp_raw = raw["digital_person"]
    digital_person = DigitalPerson(
        asset_id=dp_raw["asset_id"],
        name=dp_raw["name"],
        generator_party_id=dp_raw["generator_party_id"],
        model_provider=dp_raw["model_provider"],
        white_coat=dp_raw.get("white_coat", False),
        identity_auth_id=dp_raw.get("identity_auth_id"),
        source_prompt_ref=dp_raw.get("source_prompt_ref"),
        generation_log_ref=dp_raw.get("generation_log_ref"),
    )
    ai_raw = raw["ai_label"]
    ai_label = AiLabel(
        conspicuous_label=ai_raw.get("conspicuous_label", False),
        implicit_watermark=ai_raw.get("implicit_watermark", False),
        label_text=ai_raw.get("label_text"),
    )
    case = Case(
        case_id=raw["case_id"],
        title=raw["title"],
        advertiser_party_id=raw["advertiser_party_id"],
        beneficiary_party_id=raw.get("beneficiary_party_id"),
        product=product,
        digital_person=digital_person,
        ai_label=ai_label,
        queue=raw["queue"],
        status=CaseStatus(raw["status"]),
        created_at=raw["created_at"],
        idempotency_key=raw.get("idempotency_key"),
    )
    case.scripts = [_script(s) for s in raw.get("scripts", [])]
    case.reviews = [_review(r) for r in raw.get("reviews", [])]
    case.placements = [_placement(p) for p in raw.get("placements", [])]
    case.evidence = [_evidence(e) for e in raw.get("evidence", [])]
    case.tasks = [_task(t) for t in raw.get("tasks", [])]
    case.conflicts = [_conflict(c) for c in raw.get("conflicts", [])]
    case.events = [_event(e) for e in raw.get("events", [])]
    return case


def _script(raw: dict) -> ScriptVersion:
    return ScriptVersion(
        version=raw["version"],
        hash=raw["hash"],
        text=raw["text"],
        created_at=raw["created_at"],
        author_party_id=raw["author_party_id"],
        review_state=ReviewState(raw.get("review_state", "pending")),
        hits=[_hit(h) for h in raw.get("hits", [])],
        review_id=raw.get("review_id"),
    )


def _hit(raw: dict) -> RiskHit:
    return RiskHit(
        rule_id=raw["rule_id"],
        severity=Severity(raw["severity"]),
        category_scope=raw["category_scope"],
        term=raw["term"],
        evidence_excerpt=raw["evidence_excerpt"],
        reason=raw["reason"],
    )


def _review(raw: dict) -> ReviewOpinion:
    return ReviewOpinion(
        review_id=raw["review_id"],
        script_version=raw["script_version"],
        reviewer_user=raw["reviewer_user"],
        reviewer_role=ReviewerRole(raw["reviewer_role"]),
        decision=Decision(raw["decision"]),
        rule_version=raw["rule_version"],
        reasons=list(raw.get("reasons", [])),
        created_at=raw["created_at"],
        reused_from_review_id=raw.get("reused_from_review_id"),
        assigned_queue=raw.get("assigned_queue"),
    )


def _placement(raw: dict) -> PlacementSnapshot:
    return PlacementSnapshot(
        placement_id=raw["placement_id"],
        channel=raw["channel"],
        publisher_account=raw["publisher_account"],
        publisher_party_id=raw["publisher_party_id"],
        script_version=raw["script_version"],
        script_hash=raw["script_hash"],
        asset_id=raw["asset_id"],
        started_at=raw["started_at"],
        ended_at=raw.get("ended_at"),
        live=raw.get("live", True),
    )


def _evidence(raw: dict) -> Evidence:
    return Evidence(
        evidence_id=raw["evidence_id"],
        case_id=raw["case_id"],
        kind=raw["kind"],
        filename=raw["filename"],
        sha256=raw["sha256"],
        summary=raw["summary"],
        original_uri=raw["original_uri"],
        submitted_by_party_id=raw.get("submitted_by_party_id"),
        submitted_at=raw.get("submitted_at"),
    )


def _task(raw: dict) -> Task:
    return Task(
        task_id=raw["task_id"],
        case_id=raw["case_id"],
        kind=TaskKind(raw["kind"]),
        assignee_user=raw.get("assignee_user"),
        status=TaskStatus(raw.get("status", "open")),
        due_at=raw.get("due_at"),
        created_at=raw.get("created_at"),
        completed_at=raw.get("completed_at"),
        note=raw.get("note"),
        linked_task_id=raw.get("linked_task_id"),
    )


def _conflict(raw: dict) -> Conflict:
    return Conflict(
        code=raw["code"],
        message=raw["message"],
        related_party_ids=list(raw.get("related_party_ids", [])),
        related_product_id=raw.get("related_product_id"),
        resolved=raw.get("resolved", False),
        resolution_note=raw.get("resolution_note"),
        raised_at=raw.get("raised_at"),
    )


def _event(raw: dict) -> Event:
    return Event(
        seq=raw["seq"],
        at=raw["at"],
        actor=raw["actor"],
        action=raw["action"],
        detail=dict(raw.get("detail", {})),
    )

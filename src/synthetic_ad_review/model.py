"""数字人健康广告审查服务的领域模型。

一条广告（案件 Case）要登记九类事实：

1. 素材生成来源（数字人模型、制作者、提示词/脚本来源）
2. 人物身份授权（白大褂对应的医务身份素材由谁授权、授权范围）
3. 商品类别（药品 / 医疗器械 / 保健食品 / 普通食品 / 普通商品）
4. 脚本版本（内容哈希，改动即新版本，必须重新审核）
5. 风险词命中（命中的类别规则与原文位置）
6. AI 生成标识（显著标识 / 隐式水印是否齐备）
7. 审核意见（结论、依据、审核员）
8. 投放渠道（渠道、发布账号、脚本版本、投放版本快照）
9. 实际获利方（收款/结算主体，可与广告主、发布账号不同）

所有字段均可 JSON 序列化，便于快照持久化与跨服务恢复。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def content_hash(text: str) -> str:
    """脚本内容哈希；脚本文本任意一字之差都会产生新版本。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ProductCategory(str, Enum):
    """商品类别：不同类别套用不同的禁限规则（见 rules.py）。"""

    DRUG = "drug"                    # 药品（含非处方药）
    MEDICAL_DEVICE = "medical_device"  # 医疗器械
    HEALTH_FOOD = "health_food"      # 保健食品（蓝帽子）
    GENERAL_FOOD = "general_food"    # 普通食品
    GENERAL_GOODS = "general_goods"  # 普通商品

    @property
    def label(self) -> str:
        return {
            ProductCategory.DRUG: "药品",
            ProductCategory.MEDICAL_DEVICE: "医疗器械",
            ProductCategory.HEALTH_FOOD: "保健食品",
            ProductCategory.GENERAL_FOOD: "普通食品",
            ProductCategory.GENERAL_GOODS: "普通商品",
        }[self]


class PartyRole(str, Enum):
    ADVERTISER = "advertiser"        # 广告主（脚本/商品链接控制方）
    PRODUCER = "producer"            # 制作机构（素材生成方）
    PUBLISHER = "publisher"          # 发布者（投放账户主体）
    BENEFICIARY = "beneficiary"      # 实际获利方（收款/结算主体）
    PLATFORM = "platform"            # 平台运营者
    MEDICAL_IDENTITY_OWNER = "medical_identity_owner"  # 医务身份素材权利人

    @property
    def label(self) -> str:
        return {
            PartyRole.ADVERTISER: "广告主",
            PartyRole.PRODUCER: "制作机构",
            PartyRole.PUBLISHER: "发布者",
            PartyRole.BENEFICIARY: "实际获利方",
            PartyRole.PLATFORM: "平台运营者",
            PartyRole.MEDICAL_IDENTITY_OWNER: "身份权利人",
        }[self]


class ReviewerRole(str, Enum):
    """平台侧岗位，用于队列分配与证据分权。"""

    MODERATOR = "moderator"    # 普通审核员：只看证据摘要
    OPERATOR = "operator"      # 平台运营者：处理分配给自己的队列
    LEGAL = "legal"            # 法务：可查看证据原件
    SYSTEM = "system"          # 系统任务（时限到期等自动推进）

    @property
    def label(self) -> str:
        return {
            ReviewerRole.MODERATOR: "普通审核员",
            ReviewerRole.OPERATOR: "平台运营者",
            ReviewerRole.LEGAL: "法务",
            ReviewerRole.SYSTEM: "系统",
        }[self]


class CaseStatus(str, Enum):
    DRAFT = "draft"                    # 已登记未提交
    PENDING_REVIEW = "pending_review"  # 待审核（队列中）
    UNDER_INVESTIGATION = "under_investigation"  # 挂起调查（主体/商品冲突）
    APPROVED = "approved"              # 审核通过
    REJECTED = "rejected"              # 审核驳回
    TAKEN_DOWN = "taken_down"          # 已下架处置

    @property
    def label(self) -> str:
        return {
            CaseStatus.DRAFT: "草稿",
            CaseStatus.PENDING_REVIEW: "待审核",
            CaseStatus.UNDER_INVESTIGATION: "挂起调查",
            CaseStatus.APPROVED: "已通过",
            CaseStatus.REJECTED: "已驳回",
            CaseStatus.TAKEN_DOWN: "已下架",
        }[self]


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"  # 升级/挂起调查
    REUSE = "reuse"        # 重复提交沿用原结论


class ReviewState(str, Enum):
    """单个脚本版本的审核结论。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskKind(str, Enum):
    TAKEDOWN = "takedown"      # 下架任务
    NOTIFY = "notify"          # 通知任务（告知责任主体）
    APPEAL = "appeal"          # 申诉任务（受理与复核）
    RECTIFY = "rectify"        # 整改任务（限期补标识/删夸大用语等）

    @property
    def label(self) -> str:
        return {
            TaskKind.TAKEDOWN: "下架",
            TaskKind.NOTIFY: "通知",
            TaskKind.APPEAL: "申诉",
            TaskKind.RECTIFY: "整改",
        }[self]


class TaskStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"  # 申诉成立等情形下撤销原处置


class Severity(str, Enum):
    BANNED = "banned"        # 禁用：直接禁止发布（如"断根"）
    RESTRICTED = "restricted"  # 限制：需满足条件（如疗效用语需证明文件）
    REQUIRED = "required"    # 必备项缺失：如 AI 标识、警示语、授权、文号


@dataclass
class Party:
    """责任主体。脚本方、商品链接方、投放账户方、获利方可以是不同公司。"""

    party_id: str
    name: str
    roles: list[PartyRole]
    credit_code: str | None = None  # 统一社会信用代码


@dataclass
class IdentityAuthorization:
    """人物身份授权：白大褂/医师形象等医务身份素材的使用授权链。"""

    auth_id: str
    identity_owner_party_id: str  # 身份权利人（出镜医师或其授权机构）
    asset_id: str                 # 被授权的形象素材 ID
    scope: str                    # 授权范围（如"仅限 A 药品科普"）
    license_no: str | None        # 执业/资质编号
    valid_from: str
    valid_to: str
    on_file: bool = True          # 授权文件是否已在平台存证

    def valid_at(self, day: str) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass
class DigitalPerson:
    """数字人素材及其生成来源。"""

    asset_id: str
    name: str
    generator_party_id: str          # 生成/制作方
    model_provider: str              # 底层模型/平台
    white_coat: bool = False         # 是否呈现医务身份形象（白大褂等）
    identity_auth_id: str | None = None  # 关联的身份授权
    source_prompt_ref: str | None = None  # 提示词/驱动脚本来源存证
    generation_log_ref: str | None = None  # 生成日志存证


@dataclass
class AiLabel:
    """AI 生成标识：显著标识 + 隐式标识（元数据水印）。"""

    conspicuous_label: bool = False  # 视频画面/声音中的显著"AI生成"标识
    implicit_watermark: bool = False  # 文件元数据/隐式水印
    label_text: str | None = None

    @property
    def complete(self) -> bool:
        return self.conspicuous_label and self.implicit_watermark


@dataclass
class Product:
    """广告商品。批准文号必须与类别匹配，注册人必须是已登记主体。"""

    product_id: str
    name: str
    category: ProductCategory
    approval_no: str | None = None              # 药品批准文号/器械注册证号/食健字号
    registrant_party_id: str | None = None      # 注册人/备案主体
    link_owner_party_id: str | None = None      # 商品链接控制方
    indications: str | None = None              # 核准的功能主治/适用范围
    required_warnings: list[str] = field(default_factory=list)  # 类别要求的警示语


@dataclass
class RiskHit:
    """一次风险命中：规则编号、级别、命中词/条、在脚本中的原文片段。"""

    rule_id: str
    severity: Severity
    category_scope: str  # "all" 或具体类别
    term: str
    evidence_excerpt: str
    reason: str


@dataclass
class ScriptVersion:
    """脚本版本：内容哈希变化即新版本，必须重新审核。"""

    version: int
    hash: str
    text: str
    created_at: str
    author_party_id: str          # 脚本撰写方（可能与广告主不是同一家）
    review_state: ReviewState = ReviewState.PENDING
    hits: list[RiskHit] = field(default_factory=list)
    review_id: str | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hash": self.hash,
            "created_at": self.created_at,
            "author_party_id": self.author_party_id,
            "review_state": self.review_state.value,
            "hits": [
                {"rule_id": h.rule_id, "severity": h.severity.value, "term": h.term}
                for h in self.hits
            ],
            "review_id": self.review_id,
        }


@dataclass
class ReviewOpinion:
    """审核意见：结论 + 依据 + 审核人（用于追溯谁写了/谁放行了夸大用语）。"""

    review_id: str
    script_version: int
    reviewer_user: str
    reviewer_role: ReviewerRole
    decision: Decision
    rule_version: str
    reasons: list[str]
    created_at: str
    reused_from_review_id: str | None = None  # 沿用原结论时指向原审核
    assigned_queue: str | None = None


@dataclass
class PlacementSnapshot:
    """投放记录。素材可换账号再发，因此按 素材+渠道+账号+脚本版本 留存快照。"""

    placement_id: str
    channel: str
    publisher_account: str
    publisher_party_id: str
    script_version: int
    script_hash: str
    asset_id: str
    started_at: str
    ended_at: str | None = None
    live: bool = True


@dataclass
class Evidence:
    """证据材料。普通审核员只见摘要，法务可调原件。"""

    evidence_id: str
    case_id: str
    kind: str           # video | script_source | auth_doc | settlement | generation_log ...
    filename: str
    sha256: str
    summary: str        # 普通审核员可见的摘要（脱敏）
    original_uri: str   # 原件位置，仅法务（及调查程序）可调取
    submitted_by_party_id: str | None = None
    submitted_at: str = field(default_factory=utc_now_iso)


@dataclass
class Task:
    """处置任务：下架 / 通知 / 申诉 / 整改。整改时限用绝对时间，重启后继续推进。"""

    task_id: str
    case_id: str
    kind: TaskKind
    assignee_user: str | None      # 平台运营者：只能处理分配给自己的任务
    status: TaskStatus = TaskStatus.OPEN
    due_at: str | None = None      # 绝对截止时间（UTC ISO），服务恢复后据此判断逾期
    created_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None
    note: str | None = None
    linked_task_id: str | None = None  # 如申诉任务关联其要推翻的下架任务

    def is_overdue(self, now: datetime | None = None) -> bool:
        if self.status != TaskStatus.OPEN or self.due_at is None:
            return False
        now = now or utc_now()
        return datetime.fromisoformat(self.due_at) <= now


@dataclass
class Event:
    """案件事件流：最终据此还原完整链路。"""

    seq: int
    at: str
    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conflict:
    """主体/商品冲突记录：案件挂起调查的原因。"""

    code: str
    message: str
    related_party_ids: list[str] = field(default_factory=list)
    related_product_id: str | None = None
    resolved: bool = False
    resolution_note: str | None = None
    raised_at: str = field(default_factory=utc_now_iso)


@dataclass
class Case:
    """一条广告的审查案件，聚合九类登记信息。"""

    case_id: str
    title: str
    advertiser_party_id: str
    beneficiary_party_id: str | None     # 实际获利方：缺失即构成挂起调查事由
    product: Product
    digital_person: DigitalPerson
    ai_label: AiLabel
    queue: str                           # 分配队列
    status: CaseStatus = CaseStatus.DRAFT
    scripts: list[ScriptVersion] = field(default_factory=list)
    reviews: list[ReviewOpinion] = field(default_factory=list)
    placements: list[PlacementSnapshot] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    # 幂等键：相同素材 + 相同脚本哈希 + 相同发布账号 的重复提交沿用原结论
    idempotency_key: str | None = None

    # ---- 便捷访问 ----
    def current_script(self) -> ScriptVersion | None:
        return self.scripts[-1] if self.scripts else None

    def review_for(self, version: int) -> ReviewOpinion | None:
        for r in reversed(self.reviews):
            if r.script_version == version:
                return r
        return None

    def open_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.resolved]

    def live_placements(self) -> list[PlacementSnapshot]:
        return [p for p in self.placements if p.live]

    def record(self, actor: str, action: str, **detail: Any) -> Event:
        event = Event(seq=len(self.events) + 1, at=utc_now_iso(), actor=actor,
                      action=action, detail=detail)
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

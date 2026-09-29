"""数字人健康广告审查领域模型。

覆盖素材生成来源、人物身份授权、商品、脚本版本、风险命中、
AI 标识、审核意见、投放渠道、处置任务、申诉与责任主体。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
import types
from typing import Any, Optional, Union, get_args, get_origin


# ---------------------------------------------------------------- 枚举

class OrgRole(str, Enum):
    """公司在广告链路中可能扮演的角色。"""

    MERCHANT = "merchant"          # 商品方/广告主
    PRODUCER = "producer"          # 制作机构（素材生成来源）
    PUBLISHER = "publisher"        # 平台运营者
    BENEFICIARY = "beneficiary"    # 实际获利方


class UserRole(str, Enum):
    REVIEWER = "reviewer"   # 普通审核员：只看证据摘要
    LEGAL = "legal"         # 法务：可查看证据原件
    OPERATOR = "operator"   # 平台运营者：处理分配队列内的处置任务


class ProductCategory(str, Enum):
    DRUG = "drug"                       # 药品
    MEDICAL_DEVICE = "medical_device"   # 医疗器械
    HEALTH_FOOD = "health_food"         # 保健食品
    COSMETIC = "cosmetic"               # 化妆品
    GENERAL_FOOD = "general_food"       # 普通食品
    OTHER = "other"


class AssetType(str, Enum):
    DIGITAL_HUMAN_VIDEO = "digital_human_video"
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    TEXT = "text"


class AILabelStatus(str, Enum):
    LABELED = "labeled"          # 已添加生成合成标识
    UNLABELED = "unlabeled"      # 缺失标识
    NOT_APPLICABLE = "na"        # 非AI生成素材


class Profession(str, Enum):
    MEDICAL_STAFF = "medical_staff"   # 医务人员
    EXPERT = "expert"                 # 专家/学者
    CONSUMER = "consumer"             # 普通消费者
    UNKNOWN = "unknown"


class AuthorizationStatus(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    MISSING = "missing"
    SCOPE_MISMATCH = "scope_mismatch"


class CaseStatus(str, Enum):
    PENDING_REVIEW = "pending_review"            # 待审核
    UNDER_INVESTIGATION = "under_investigation"  # 主体/商品冲突，挂起调查
    APPROVED = "approved"                        # 审核通过
    REJECTED = "rejected"                        # 审核驳回
    REVOKED = "revoked"                          # 投放后被撤销，须下架整改
    CLOSED = "closed"                            # 处置完成结案


class DecisionType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    SUSPEND = "suspend"   # 挂起调查


class Severity(str, Enum):
    BLOCK = "block"   # 命中即驳回
    WARN = "warn"     # 需人工复核/整改


class TaskType(str, Enum):
    TAKEDOWN = "takedown"   # 下架
    NOTIFY = "notify"       # 通知责任主体
    RECTIFY = "rectify"     # 限期整改


class TaskStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    OVERDUE = "overdue"      # 已逾期，自动升级
    ESCALATED = "escalated"  # 升级处理
    CANCELED = "canceled"    # 申诉成立等原因撤销


class PlacementStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class AppealStatus(str, Enum):
    FILED = "filed"
    REVIEWING = "reviewing"
    UPHELD = "upheld"         # 维持原结论
    OVERTURNED = "overturned" # 申诉成立，撤销处置


# ---------------------------------------------------------------- 主体

@dataclass
class Org:
    """公司主体：脚本、商品链接、投放账户可能分属不同公司。"""

    org_id: str
    name: str
    roles: set[OrgRole] = field(default_factory=set)


@dataclass
class User:
    user_id: str
    name: str
    org_id: str
    roles: set[UserRole] = field(default_factory=set)
    queues: set[str] = field(default_factory=set)  # 可处理的队列编号


# ---------------------------------------------------------------- 身份与授权

@dataclass
class IdentityAuthorization:
    """人物身份/肖像的授权记录。"""

    auth_id: str
    licensor: str                       # 授权方（身份本人或其所属机构）
    licensee_org_id: str                # 被授权使用素材的公司
    scope: str                          # 授权范围（用途、素材类型）
    valid_from: datetime
    valid_to: datetime
    document_ref: str                   # 授权书原件存放位置


@dataclass
class PersonaIdentity:
    """数字人/出镜人物所使用的身份与医务身份素材。"""

    identity_id: str
    name: str
    profession: Profession
    real_person: bool                   # 是否对应真实自然人
    owner_org_id: str                   # 身份素材归属公司
    evidence_refs: list[str] = field(default_factory=list)  # 原件（执业证等）
    authorizations: list[IdentityAuthorization] = field(default_factory=list)


@dataclass
class Asset:
    """广告素材本体。"""

    asset_id: str
    asset_type: AssetType
    producer_org_id: str                # 生成来源/制作机构
    generator_tool: str                 # 生成工具或模型
    generated_at: datetime
    ai_label: AILabelStatus
    content_hash: str
    summary: str = ""                   # 证据摘要（普通审核员可见）
    persona_identity_id: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)  # 原件（仅法务可见）


# ---------------------------------------------------------------- 商品与脚本

@dataclass
class Product:
    product_id: str
    name: str
    category: ProductCategory
    registrant_org_id: str              # 注册/备案主体
    approval_no: str                    # 批准文号/备案号
    link_org_id: str                    # 控制商品链接的公司
    link_url: str = ""


@dataclass
class ScriptVersion:
    """不可变的脚本版本；任何修改都会产生新版本并触发重审。"""

    version_id: str
    case_id: str
    content: str
    content_hash: str
    author_org_id: str                  # 脚本撰写方
    created_at: datetime
    supersedes: Optional[str] = None    # 上一版本编号


@dataclass
class RiskHit:
    rule_id: str
    severity: Severity
    category: str
    message: str
    matched_terms: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 审核与投放

@dataclass
class ReviewDecision:
    decision_id: str
    case_id: str
    script_version_id: str
    reviewer_user_id: str
    rule_pack_version: str
    decision: DecisionType
    hits: list[RiskHit]
    comments: str
    created_at: datetime
    reused_from: Optional[str] = None   # 重复提交时沿用的原结论编号


@dataclass
class Placement:
    """实际投放记录：渠道、账户控制方与投放的脚本版本。"""

    placement_id: str
    case_id: str
    channel: str
    account_org_id: str                 # 投放账户控制公司
    account_name: str
    script_version_id: str              # 已投放的版本（用于追溯）
    status: PlacementStatus
    placed_at: datetime
    removed_at: Optional[datetime] = None


@dataclass
class EnforcementTask:
    """下架、通知、限期整改任务；时限为绝对时间，服务重启后继续计算。"""

    task_id: str
    case_id: str
    task_type: TaskType
    assignee_queue: str
    created_at: datetime
    deadline: datetime
    status: TaskStatus = TaskStatus.PENDING
    assignee_user_id: Optional[str] = None
    result_note: str = ""
    completed_at: Optional[datetime] = None
    escalated_at: Optional[datetime] = None


@dataclass
class Appeal:
    appeal_id: str
    case_id: str
    filed_by_org_id: str
    filed_at: datetime
    reason: str
    status: AppealStatus = AppealStatus.FILED
    resolution_note: str = ""
    resolved_at: Optional[datetime] = None


# ---------------------------------------------------------------- 案件

@dataclass
class Case:
    """一条广告从素材到处置的完整聚合根。"""

    case_id: str
    title: str
    asset_id: str
    product_id: str
    merchant_org_id: str                # 广告主/商品方（提交方）
    beneficiary_org_id: str             # 实际获利方
    submission_key: str                 # 幂等键：相同素材+脚本+商品+渠道
    status: CaseStatus
    created_at: datetime
    current_script_version_id: Optional[str] = None
    placement_ids: list[str] = field(default_factory=list)
    duplicate_of: Optional[str] = None  # 重复提交指向的原案件
    conflict_notes: list[str] = field(default_factory=list)
    closed_at: Optional[datetime] = None


# ---------------------------------------------------------------- 序列化

_BUILTIN_SCALARS = {str, int, float, bool}


def _encode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, set):
        return sorted(_encode(v) for v in value)
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _resolve(hint: Any) -> Any:
    """把字符串形式的注解（PEP 563）解析回真实类型。"""
    if isinstance(hint, str):
        return eval(hint, globals())  # noqa: S307 - 仅解析本模块内定义的类型名
    return hint


def _decode(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    hint = _resolve(hint)
    origin = get_origin(hint)
    if origin in (Union, getattr(types, "UnionType", None)):
        args = [a for a in get_args(hint) if a is not type(None)]
        return _decode(args[0], value)
    if origin in (list, set):
        inner = get_args(hint)[0]
        decoded = [_decode(inner, v) for v in value]
        return set(decoded) if origin is set else decoded
    if origin is dict:
        return value
    if hint in _BUILTIN_SCALARS:
        return value
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    if hint is datetime:
        return datetime.fromisoformat(value)
    if is_dataclass(hint):
        import typing
        resolved = typing.get_type_hints(hint)
        kwargs = {}
        for f in fields(hint):
            if f.name in value:
                kwargs[f.name] = _decode(resolved[f.name], value[f.name])
        return hint(**kwargs)
    return value


def to_dict(value: Any) -> Any:
    return _encode(value)


def from_dict(model_cls: type, value: Any) -> Any:
    return _decode(model_cls, value)

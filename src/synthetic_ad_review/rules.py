"""按商品类别套用的禁限规则与风险词命中。

规则以版本化规则包（RulePack）形式发布；审核结论固定当时的规则包版本，
保证已投放版本日后仍可按当时规则追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    AILabelStatus,
    Asset,
    AssetType,
    AuthorizationStatus,
    PersonaIdentity,
    Product,
    ProductCategory,
    Profession,
    RiskHit,
    ScriptVersion,
    Severity,
)

RULE_PACK_VERSION = "rules-2026.09"


@dataclass(frozen=True)
class KeywordRule:
    rule_id: str
    severity: Severity
    categories: frozenset[ProductCategory]
    terms: tuple[str, ...]
    message: str


# 根治疗效承诺：药品也禁止表示功效的断言或保证，普通商品更不允许
_CURE_TERMS = ("断根", "根治", "包治", "治愈率", "有效率", "药到病除", "无效退款", "一盒见效", "彻底治愈")

# 绝对化用语：所有类别一律命中，需举证或删除
_ABSOLUTE_TERMS = ("国家级", "最高级", "最佳", "第一品牌", "顶级", "万能", "永不复发")

# 疾病治疗表述：保健品、普通食品、化妆品不得宣称
_DISEASE_TERMS = (
    "治疗", "疗效", "医治", "治愈", "处方", "降压", "降糖", "抗肿瘤",
    "消炎", "镇痛", "康复病例",
)

KEYWORD_RULES: tuple[KeywordRule, ...] = (
    KeywordRule(
        rule_id="KW-CURE-001",
        severity=Severity.BLOCK,
        categories=frozenset(ProductCategory),
        terms=_CURE_TERMS,
        message="含有根治/治愈率等功效保证用语，涉嫌虚假或违禁宣传",
    ),
    KeywordRule(
        rule_id="KW-ABS-001",
        severity=Severity.WARN,
        categories=frozenset(ProductCategory),
        terms=_ABSOLUTE_TERMS,
        message="含绝对化用语，需提供证明材料或删除",
    ),
    KeywordRule(
        rule_id="KW-DIS-001",
        severity=Severity.BLOCK,
        categories=frozenset({
            ProductCategory.HEALTH_FOOD,
            ProductCategory.GENERAL_FOOD,
            ProductCategory.COSMETIC,
        }),
        terms=_DISEASE_TERMS,
        message="该类别商品不得宣称疾病治疗功能",
    ),
    KeywordRule(
        rule_id="KW-HF-001",
        severity=Severity.BLOCK,
        categories=frozenset({ProductCategory.HEALTH_FOOD}),
        terms=("替代药物", "代替药物", "辅助治疗"),
        message="保健食品不得声称替代药物或辅助治疗",
    ),
)

# 药品、医疗器械不得利用广告代言人作推荐、证明（含医务人员形象）
NO_ENDORSER_CATEGORIES = frozenset({
    ProductCategory.DRUG,
    ProductCategory.MEDICAL_DEVICE,
})

HEALTH_FOOD_DISCLAIMER = "本品不能代替药物"


@dataclass
class Evaluation:
    """一次完整规则评估的输入快照与命中结果。"""

    hits: list[RiskHit] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(h.severity is Severity.BLOCK for h in self.hits)


def _keyword_hits(script: ScriptVersion, product: Product) -> list[RiskHit]:
    hits: list[RiskHit] = []
    for rule in KEYWORD_RULES:
        if product.category not in rule.categories:
            continue
        matched = [t for t in rule.terms if t in script.content]
        if matched:
            hits.append(
                RiskHit(
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    category=product.category.value,
                    message=rule.message,
                    matched_terms=matched,
                )
            )
    return hits


def _check_persona(
    product: Product,
    asset: Asset,
    persona: PersonaIdentity | None,
    auth_status: AuthorizationStatus | None,
) -> list[RiskHit]:
    hits: list[RiskHit] = []

    # 药品/医疗器械：任何代言人推荐证明都被禁止，医务人员形象更是直接违规
    if persona is not None and product.category in NO_ENDORSER_CATEGORIES:
        if persona.profession is Profession.MEDICAL_STAFF:
            hits.append(RiskHit(
                rule_id="ID-MED-001",
                severity=Severity.BLOCK,
                category=product.category.value,
                message="药品、医疗器械广告不得利用医务人员形象作推荐证明",
            ))
        else:
            hits.append(RiskHit(
                rule_id="ID-END-001",
                severity=Severity.BLOCK,
                category=product.category.value,
                message="药品、医疗器械广告不得利用广告代言人作推荐、证明",
            ))

    # 其他类别穿白大褂宣称专业身份，需要真实授权且身份属实，否则按虚构身份处理
    if (
        persona is not None
        and product.category not in NO_ENDORSER_CATEGORIES
        and persona.profession is Profession.MEDICAL_STAFF
        and not persona.real_person
    ):
        hits.append(RiskHit(
            rule_id="ID-FAKE-001",
            severity=Severity.BLOCK,
            category=product.category.value,
            message="数字人虚构医务人员身份，属于虚假代言",
        ))

    # 授权状态
    if persona is not None:
        if auth_status is AuthorizationStatus.MISSING:
            hits.append(RiskHit(
                rule_id="ID-AUTH-001",
                severity=Severity.BLOCK,
                category=product.category.value,
                message="缺少人物身份/肖像授权文件",
            ))
        elif auth_status is AuthorizationStatus.EXPIRED:
            hits.append(RiskHit(
                rule_id="ID-AUTH-002",
                severity=Severity.BLOCK,
                category=product.category.value,
                message="人物身份授权已过期",
            ))
        elif auth_status is AuthorizationStatus.SCOPE_MISMATCH:
            hits.append(RiskHit(
                rule_id="ID-AUTH-003",
                severity=Severity.BLOCK,
                category=product.category.value,
                message="授权范围与本次素材用途不符",
            ))

    return hits


def _check_asset(product: Product, asset: Asset) -> list[RiskHit]:
    hits: list[RiskHit] = []
    if asset.asset_type is AssetType.DIGITAL_HUMAN_VIDEO and asset.ai_label is AILabelStatus.UNLABELED:
        hits.append(RiskHit(
            rule_id="AI-LBL-001",
            severity=Severity.BLOCK,
            category=product.category.value,
            message="AI生成的数字人视频缺少显著的生成合成标识",
        ))
    return hits


def _check_disclaimer(script: ScriptVersion, product: Product) -> list[RiskHit]:
    hits: list[RiskHit] = []
    if product.category is ProductCategory.HEALTH_FOOD and HEALTH_FOOD_DISCLAIMER not in script.content:
        hits.append(RiskHit(
            rule_id="HF-DISC-001",
            severity=Severity.WARN,
            category=product.category.value,
            message=f"保健食品广告应标注提示语「{HEALTH_FOOD_DISCLAIMER}」",
        ))
    return hits


def evaluate(
    script: ScriptVersion,
    product: Product,
    asset: Asset,
    persona: PersonaIdentity | None,
    auth_status: AuthorizationStatus | None,
    *,
    rule_pack_version: str = RULE_PACK_VERSION,
    evaluated_at: datetime | None = None,  # 预留：规则包随时间变化时按审核时刻取规则
) -> Evaluation:
    """对一个脚本版本套用全部禁限规则，返回命中清单。"""
    hits: list[RiskHit] = []
    hits.extend(_keyword_hits(script, product))
    hits.extend(_check_persona(product, asset, persona, auth_status))
    hits.extend(_check_asset(product, asset))
    hits.extend(_check_disclaimer(script, product))
    return Evaluation(hits=hits)

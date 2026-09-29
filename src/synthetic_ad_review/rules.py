"""分类别禁限规则。

规则集版本化（RULEBOOK_VERSION），审核意见记录所依据的规则版本。
依据：广告法第 9/16/17/18 条、《人工智能生成合成内容标识办法》（2025-09-01 施行）
及平台数字人广告审查细则；本项目为演示实现，词条可在规则集中增删。
"""

from __future__ import annotations

from dataclasses import dataclass
from .model import (
    AiLabel,
    DigitalPerson,
    Product,
    ProductCategory,
    RiskHit,
    Severity,
)

RULEBOOK_VERSION = "rb-2026.09"

ALL = "all"


@dataclass(frozen=True)
class TermRule:
    rule_id: str
    severity: Severity
    categories: tuple[str, ...]  # ALL 表示适用于全部类别
    terms: tuple[str, ...]
    reason: str


# 词条规则：按顺序匹配，同一条脚本可命中多条
TERM_RULES: tuple[TermRule, ...] = (
    TermRule(
        "R-DRUG-01", Severity.BANNED,
        (ProductCategory.DRUG.value, ProductCategory.MEDICAL_DEVICE.value),
        ("断根", "根治", "包治", "包治百病", "药到病除", "彻底治愈", "永不复发",
         "治愈率", "有效率", "100%有效", "无效退款", "无毒副作用", "最安全", "安全无副作用"),
        "药品、医疗器械广告不得含有表示功效、安全性的断言或保证，不得说明治愈率、有效率",
    ),
    TermRule(
        "R-ENDORSE-01", Severity.BANNED,
        (ProductCategory.DRUG.value, ProductCategory.MEDICAL_DEVICE.value),
        ("医生推荐", "医师推荐", "专家推荐", "院长推荐", "医务人员推荐"),
        "药品、医疗器械广告不得利用医务人员或代言人作推荐、证明",
    ),
    TermRule(
        "R-HEALTH-01", Severity.BANNED,
        (ProductCategory.HEALTH_FOOD.value, ProductCategory.GENERAL_FOOD.value),
        ("治疗", "治愈", "断根", "根治", "疗效", "医治", "防癌", "抗癌",
         "预防疾病", "替代药物", "包治", "药到病除"),
        "保健食品、普通食品不得涉及疾病预防、治疗功能",
    ),
    TermRule(
        "R-MEDCLAIM-01", Severity.BANNED,
        (ProductCategory.GENERAL_FOOD.value, ProductCategory.GENERAL_GOODS.value),
        ("断根", "根治", "治愈", "治疗", "疗效", "医治", "诊疗", "药到病除",
         "无毒副作用", "安全无副作用"),
        "普通食品、普通商品（含消字号产品）不得使用医疗用语或宣称疾病治疗功效",
    ),
    TermRule(
        "R-HEALTH-02", Severity.RESTRICTED,
        (ProductCategory.HEALTH_FOOD.value,),
        ("增强免疫力", "辅助降血糖", "辅助降血脂", "减肥"),
        "保健食品仅可声称经批准的保健功能，且需与注册批件一致",
    ),
    TermRule(
        "R-ABS-01", Severity.BANNED,
        (ALL,),
        ("国家级", "最高级", "最佳", "第一品牌", "全网第一", "顶级", "绝无仅有"),
        "广告不得使用绝对化用语",
    ),
    TermRule(
        "R-EXPERIENCE-01", Severity.RESTRICTED,
        (ALL,),
        ("亲测", "亲测有效", "我用了之后", "亲身体验", "用了一周就好", "患者自述"),
        "数字人虚构使用体验需有真实使用者授权与凭证，否则按虚假宣传处理",
    ),
)

# 各类别必须随广告展示的警示语（必须出现在脚本中）
CATEGORY_REQUIRED_WARNINGS: dict[ProductCategory, tuple[str, ...]] = {
    ProductCategory.HEALTH_FOOD: ("本品不能代替药物",),
    ProductCategory.DRUG: ("请按药品说明书或者在药师指导下购买和使用",),
}

# 必须具备批准/注册/备案文号的类别
APPROVAL_REQUIRED_CATEGORIES = (
    ProductCategory.DRUG,
    ProductCategory.MEDICAL_DEVICE,
    ProductCategory.HEALTH_FOOD,
)

# 批准文号前缀与类别对应关系（用于商品类别冲突检测）
APPROVAL_PREFIXES: dict[ProductCategory, tuple[str, ...]] = {
    ProductCategory.DRUG: ("国药准字",),
    ProductCategory.MEDICAL_DEVICE: ("国械注", "省械注", "械注"),
    ProductCategory.HEALTH_FOOD: ("食健备", "食健字", "国食健字", "卫食健字"),
}


def _excerpt(text: str, term: str, radius: int = 12) -> str:
    idx = text.find(term)
    if idx < 0:
        return term
    start = max(0, idx - radius)
    end = min(len(text), idx + len(term) + radius)
    return text[start:end].replace("\n", " ")


def scan_terms(category: ProductCategory, text: str) -> list[RiskHit]:
    """按词条规则扫描脚本文本，返回风险命中。"""
    hits: list[RiskHit] = []
    for rule in TERM_RULES:
        if ALL not in rule.categories and category.value not in rule.categories:
            continue
        for term in rule.terms:
            pos = text.find(term)
            if pos >= 0:
                hits.append(RiskHit(
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    category_scope=category.value,
                    term=term,
                    evidence_excerpt=_excerpt(text, term),
                    reason=rule.reason,
                ))
                break  # 一条规则只记录一次首要命中，避免重复
    return hits


def scan_structure(
    category: ProductCategory,
    text: str,
    product: Product,
    digital_person: DigitalPerson,
    ai_label: AiLabel,
    has_valid_identity_auth: bool,
) -> list[RiskHit]:
    """结构性检查：标识、身份授权、代言禁令、文号、警示语。

    与词条无关，只看素材/商品/脚本的合规要件是否齐备。
    """
    hits: list[RiskHit] = []

    # R-AI-01：AI 生成标识必须齐备（显著标识 + 隐式水印）
    missing = []
    if not ai_label.conspicuous_label:
        missing.append("显著标识")
    if not ai_label.implicit_watermark:
        missing.append("隐式水印")
    if missing:
        hits.append(RiskHit(
            "R-AI-01", Severity.REQUIRED, category.value,
            term="AI标识缺失",
            evidence_excerpt=f"缺少：{'、'.join(missing)}",
            reason="AI 生成合成内容应当同时具备显著标识与隐式标识",
        ))

    # R-AUTH-01：白大褂等医务身份形象必须有有效授权链
    if digital_person.white_coat and not has_valid_identity_auth:
        hits.append(RiskHit(
            "R-AUTH-01", Severity.REQUIRED, category.value,
            term="医务身份无授权",
            evidence_excerpt=f"素材 {digital_person.asset_id} 呈现白大褂形象，未核验到有效身份授权",
            reason="使用医务人员形象须取得身份权利人授权且在授权范围与有效期内",
        ))

    # R-ENDORSE-02：药品、医疗器械即使授权齐备，也不得利用医务人员形象推荐证明
    if category in (ProductCategory.DRUG, ProductCategory.MEDICAL_DEVICE) and digital_person.white_coat:
        hits.append(RiskHit(
            "R-ENDORSE-02", Severity.BANNED, category.value,
            term="医务人员形象代言",
            evidence_excerpt=f"素材 {digital_person.asset_id} 以白大褂形象为{category.label}作推荐证明",
            reason="药品、医疗器械广告不得利用广告代言人（含医务人员形象的数字人）作推荐、证明",
        ))

    # R-APPROVAL-01：药品/器械/保健食品必须有文号
    if category in APPROVAL_REQUIRED_CATEGORIES and not product.approval_no:
        hits.append(RiskHit(
            "R-APPROVAL-01", Severity.REQUIRED, category.value,
            term="批准文号缺失",
            evidence_excerpt=f"商品 {product.product_id}（{category.label}）未登记批准/注册/备案文号",
            reason=f"{category.label}广告须核对并展示有效批准文号",
        ))

    # R-WARN-xx：类别警示语必须出现在脚本中
    for warning in CATEGORY_REQUIRED_WARNINGS.get(category, ()):  # type: ignore[arg-type]
        if warning not in text:
            hits.append(RiskHit(
                "R-WARN-01", Severity.REQUIRED, category.value,
                term="警示语缺失",
                evidence_excerpt=f"脚本未包含法定提示语：{warning}",
                reason=f"{category.label}广告应当显著标明：{warning}",
            ))

    return hits


def scan(
    category: ProductCategory,
    text: str,
    product: Product,
    digital_person: DigitalPerson,
    ai_label: AiLabel,
    has_valid_identity_auth: bool,
) -> list[RiskHit]:
    """词条 + 结构一并扫描，按规则编号排序输出。"""
    hits = scan_terms(category, text) + scan_structure(
        category, text, product, digital_person, ai_label, has_valid_identity_auth
    )
    return sorted(hits, key=lambda h: (h.rule_id, h.term))


def has_blocking_hit(hits: list[RiskHit]) -> bool:
    """存在任一禁用命中即不得放行。"""
    return any(h.severity is Severity.BANNED for h in hits)


def approval_prefix_matches(category: ProductCategory, approval_no: str) -> bool:
    """文号前缀必须与商品类别一致，否则属于商品/文号冲突。"""
    prefixes = APPROVAL_PREFIXES.get(category, ())
    return any(approval_no.startswith(p) for p in prefixes)

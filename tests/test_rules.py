"""分类别禁限规则测试。"""

import unittest

from src.synthetic_ad_review.model import (
    AiLabel,
    ProductCategory,
    Severity,
)
from src.synthetic_ad_review.rules import (
    RULEBOOK_VERSION,
    approval_prefix_matches,
    scan,
)
from tests._fixtures import digital_person, product


def rule_ids(hits):
    return {h.rule_id for h in hits}


class RulesTest(unittest.TestCase):
    def test_drug_banned_terms(self):
        hits = scan(
            ProductCategory.DRUG, "此药抹上七天断根，药到病除，永不复发",
            product(), digital_person(), AiLabel(True, True), True,
        )
        self.assertIn("R-DRUG-01", rule_ids(hits))
        self.assertTrue(any(h.severity is Severity.BANNED for h in hits))

    def test_drug_white_coat_endorsement_banned_even_with_auth(self):
        # 即使身份授权有效，药品/器械也不得用医务人员形象代言
        hits = scan(
            ProductCategory.DRUG, "遵医嘱使用。",
            product(), digital_person(white_coat=True, identity_auth_id="AUTH-1"),
            AiLabel(True, True), True,
        )
        self.assertIn("R-ENDORSE-02", rule_ids(hits))

    def test_white_coat_without_auth_requires_auth(self):
        hits = scan(
            ProductCategory.HEALTH_FOOD, "大家好。",
            product(ProductCategory.HEALTH_FOOD, "国食健字G20240001"),
            digital_person(white_coat=True), AiLabel(True, True), False,
        )
        self.assertIn("R-AUTH-01", rule_ids(hits))

    def test_ai_label_required(self):
        hits = scan(
            ProductCategory.GENERAL_GOODS, "普通介绍。",
            product(ProductCategory.GENERAL_GOODS, None),
            digital_person(), AiLabel(False, True), True,
        )
        self.assertIn("R-AI-01", rule_ids(hits))
        hit = next(h for h in hits if h.rule_id == "R-AI-01")
        self.assertIn("显著标识", hit.evidence_excerpt)

    def test_health_food_disease_claim_and_warning(self):
        base = product(ProductCategory.HEALTH_FOOD, "国食健字G20240001")
        hits = scan(
            ProductCategory.HEALTH_FOOD, "喝了能治疗糖尿病，替代药物！",
            base, digital_person(), AiLabel(True, True), True,
        )
        self.assertIn("R-HEALTH-01", rule_ids(hits))
        self.assertIn("R-WARN-01", rule_ids(hits))

        # 加上法定提示语后警示语命中消失
        hits2 = scan(
            ProductCategory.HEALTH_FOOD, "具有辅助降血糖功能。本品不能代替药物。",
            base, digital_person(), AiLabel(True, True), True,
        )
        self.assertNotIn("R-WARN-01", rule_ids(hits2))
        self.assertIn("R-HEALTH-02", rule_ids(hits2))  # 限制性声称仍需凭证

    def test_general_goods_medical_terms(self):
        # 消字号普通商品宣称断根：命中普通商品医疗用语规则
        hits = scan(
            ProductCategory.GENERAL_GOODS, "抹上七天皮炎断根",
            product(ProductCategory.GENERAL_GOODS, "粤卫消证字(2025)第1号"),
            digital_person(white_coat=True), AiLabel(False, False), False,
        )
        self.assertIn("R-MEDCLAIM-01", rule_ids(hits))
        self.assertIn("R-AI-01", rule_ids(hits))
        self.assertIn("R-AUTH-01", rule_ids(hits))

    def test_absolute_and_experience_terms_apply_all(self):
        hits = scan(
            ProductCategory.GENERAL_GOODS, "全网第一，我亲测有效",
            product(ProductCategory.GENERAL_GOODS, None),
            digital_person(), AiLabel(True, True), True,
        )
        self.assertIn("R-ABS-01", rule_ids(hits))
        self.assertIn("R-EXPERIENCE-01", rule_ids(hits))

    def test_approval_required_for_regulated_categories(self):
        hits = scan(
            ProductCategory.MEDICAL_DEVICE, "合规介绍文本。",
            product(ProductCategory.MEDICAL_DEVICE, None),
            digital_person(), AiLabel(True, True), True,
        )
        self.assertIn("R-APPROVAL-01", rule_ids(hits))

    def test_approval_prefix_matches(self):
        self.assertTrue(approval_prefix_matches(ProductCategory.DRUG, "国药准字H20260001"))
        self.assertTrue(approval_prefix_matches(
            ProductCategory.MEDICAL_DEVICE, "国械注准20263100001"))
        self.assertTrue(approval_prefix_matches(
            ProductCategory.HEALTH_FOOD, "国食健字G20240188"))
        self.assertFalse(approval_prefix_matches(ProductCategory.DRUG, "国食健字G20240188"))

    def test_rulebook_versioned(self):
        self.assertTrue(RULEBOOK_VERSION.startswith("rb-"))


if __name__ == "__main__":
    unittest.main()

"""测试公共构造：可拨快时钟、已登记主体与岗位用户。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.synthetic_ad_review.access import User
from src.synthetic_ad_review.model import (
    AiLabel,
    DigitalPerson,
    Party,
    PartyRole,
    Product,
    ProductCategory,
    ReviewerRole,
)
from src.synthetic_ad_review.service import ReviewService

T0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def build_service(clock: FakeClock | None = None) -> tuple[ReviewService, FakeClock]:
    clock = clock or FakeClock()
    svc = ReviewService(clock=clock)
    parties = [
        Party("P-ADV", "甲广告主有限公司", [PartyRole.ADVERTISER]),
        Party("P-PUB", "乙发布科技有限公司", [PartyRole.PUBLISHER]),
        Party("P-PUB2", "丙矩阵营销中心", [PartyRole.PUBLISHER]),
        Party("P-BEN", "丁实际获利有限公司", [PartyRole.BENEFICIARY]),
        Party("P-STUDIO", "戊数字人制作有限公司", [PartyRole.PRODUCER]),
        Party("P-LINK", "己电商链接有限公司", []),
    ]
    for party in parties:
        svc.register_party(party)
    return svc, clock


def users() -> dict[str, User]:
    return {
        "op_q1": User("op_q1", ReviewerRole.OPERATOR, queues=("q1",)),
        "op_q2": User("op_q2", ReviewerRole.OPERATOR, queues=("q2",)),
        "mod_q1": User("mod_q1", ReviewerRole.MODERATOR, queues=("q1",)),
        "legal": User("legal", ReviewerRole.LEGAL, queues=("q1", "q2", "patrol")),
        "patrol": User("patrol", ReviewerRole.OPERATOR, queues=("patrol", "q1")),
        "insider": User("insider", ReviewerRole.OPERATOR,
                        queues=("q1",), affiliated_party_ids=("P-ADV",)),
    }


def digital_person(asset_id: str = "ASSET-1", white_coat: bool = False,
                   identity_auth_id: str | None = None) -> DigitalPerson:
    return DigitalPerson(
        asset_id=asset_id,
        name="测试数字人",
        generator_party_id="P-STUDIO",
        model_provider="测试模型-X",
        white_coat=white_coat,
        identity_auth_id=identity_auth_id,
        source_prompt_ref="oss://prompt/x.json",
        generation_log_ref="oss://genlog/x/",
    )


def product(category: ProductCategory = ProductCategory.DRUG,
            approval_no: str | None = "国药准字H20260001",
            product_id: str = "G-1") -> Product:
    return Product(
        product_id=product_id,
        name="测试商品",
        category=category,
        approval_no=approval_no,
        registrant_party_id="P-ADV",
        link_owner_party_id="P-LINK",
    )


def full_ai_label() -> AiLabel:
    return AiLabel(True, True, label_text="本内容由 AI 生成")

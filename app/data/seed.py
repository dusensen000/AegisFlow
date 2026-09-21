"""人为构造的订单、物流、售后规则与工单数据。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    AfterSaleRule,
    LogisticsEvent,
    LogisticsInfo,
    Order,
    OrderStatus,
    Ticket,
)


ORDERS: list[Order] = [
    Order(
        order_id="SO20260101001",
        user_id="U10001",
        product_name="无线蓝牙耳机",
        amount=299.0,
        status=OrderStatus.DELIVERED,
        created_at="2026-01-05 10:12:00",
        payment_method="支付宝",
        after_sale_count=0,
        protection_expired=False,
    ),
    Order(
        order_id="SO20260114002",
        user_id="U10002",
        product_name="智能手环",
        amount=399.0,
        status=OrderStatus.SHIPPED,
        created_at="2026-01-14 14:30:00",
        payment_method="微信支付",
        after_sale_count=0,
        protection_expired=False,
    ),
    Order(
        order_id="SO20260120003",
        user_id="U10003",
        product_name="便携充电宝",
        amount=129.0,
        status=OrderStatus.DELIVERED,
        created_at="2026-01-20 09:08:00",
        payment_method="银行卡",
        after_sale_count=1,
        protection_expired=False,
    ),
    Order(
        order_id="SO20251228004",
        user_id="U10004",
        product_name="机械键盘",
        amount=499.0,
        status=OrderStatus.DELIVERED,
        created_at="2025-12-28 18:45:00",
        payment_method="支付宝",
        after_sale_count=0,
        protection_expired=True,
    ),
    Order(
        order_id="SO20260201005",
        user_id="U10005",
        product_name="保温杯",
        amount=89.0,
        status=OrderStatus.PENDING,
        created_at="2026-02-01 11:20:00",
        payment_method="微信支付",
        after_sale_count=0,
        protection_expired=False,
    ),
    Order(
        order_id="SO20260110006",
        user_id="U10006",
        product_name="降噪耳机",
        amount=699.0,
        status=OrderStatus.DELIVERED,
        created_at="2026-01-10 20:02:00",
        payment_method="支付宝",
        after_sale_count=0,
        protection_expired=False,
    ),
]


LOGISTICS: list[LogisticsInfo] = [
    LogisticsInfo(
        order_id="SO20260101001",
        carrier="顺丰速运",
        tracking_no="SF20260101001",
        current_status="已签收",
        events=[
            LogisticsEvent(time="2026-01-05 18:00", status="已揽收", location="上海"),
            LogisticsEvent(time="2026-01-07 09:30", status="运输中", location="杭州"),
            LogisticsEvent(time="2026-01-08 14:10", status="已签收", location="天津"),
        ],
    ),
    LogisticsInfo(
        order_id="SO20260114002",
        carrier="中通快递",
        tracking_no="ZT20260114002",
        current_status="运输中",
        events=[
            LogisticsEvent(time="2026-01-14 16:00", status="已揽收", location="广州"),
            LogisticsEvent(time="2026-01-15 10:20", status="运输中", location="武汉"),
        ],
    ),
    LogisticsInfo(
        order_id="SO20260120003",
        carrier="圆通速递",
        tracking_no="YT20260120003",
        current_status="已签收",
        events=[
            LogisticsEvent(time="2026-01-20 12:00", status="已揽收", location="深圳"),
            LogisticsEvent(time="2026-01-22 17:40", status="已签收", location="北京"),
        ],
    ),
    LogisticsInfo(
        order_id="SO20251228004",
        carrier="京东物流",
        tracking_no="JD20251228004",
        current_status="已签收",
        events=[
            LogisticsEvent(time="2025-12-28 20:00", status="已揽收", location="上海"),
            LogisticsEvent(time="2025-12-30 11:00", status="已签收", location="成都"),
        ],
    ),
    LogisticsInfo(
        order_id="SO20260201005",
        carrier="待发货",
        tracking_no="",
        current_status="未发货",
        events=[],
    ),
    LogisticsInfo(
        order_id="SO20260110006",
        carrier="顺丰速运",
        tracking_no="SF20260110006",
        current_status="已签收",
        events=[
            LogisticsEvent(time="2026-01-10 21:00", status="已揽收", location="北京"),
            LogisticsEvent(time="2026-01-12 10:00", status="运输中", location="郑州"),
            LogisticsEvent(time="2026-01-13 16:30", status="已签收", location="西安"),
        ],
    ),
]


RULES: list[AfterSaleRule] = [
    AfterSaleRule(
        rule_id="R001",
        category="退款",
        condition="订单已签收且签收时间在 7 天内",
        description="签收后 7 天内支持无理由退款，退款金额按实付金额原路退回",
        permission_level=1,
        actions=["原路退款", "关闭售后"],
    ),
    AfterSaleRule(
        rule_id="R002",
        category="退货",
        condition="订单已签收且签收时间在 15 天内",
        description="签收后 15 天内支持无理由退货，退回商品需保持完好",
        permission_level=1,
        actions=["退货退款", "生成退货地址"],
    ),
    AfterSaleRule(
        rule_id="R003",
        category="换货",
        condition="订单已签收且签收时间在 30 天内，商品存在质量问题",
        description="签收后 30 天内出现质量问题支持换货，商家承担运费",
        permission_level=1,
        actions=["换货", "生成换货单"],
    ),
    AfterSaleRule(
        rule_id="R004",
        category="补偿",
        condition="物流信息超过 48 小时未更新或预计送达时间延误",
        description="物流异常导致等待时间过长，可申请小额体验补偿",
        permission_level=1,
        actions=["发放补偿优惠券", "补偿 5%-10% 订单金额"],
    ),
    AfterSaleRule(
        rule_id="R005",
        category="仅退款",
        condition="订单未发货或商品少件，无需退回商品",
        description="未发货或确认少件时支持仅退款",
        permission_level=1,
        actions=["仅退款", "关闭售后"],
    ),
    AfterSaleRule(
        rule_id="R006",
        category="补偿",
        condition="商品在运输途中破损或到货外观损坏",
        description="根据破损程度补偿 10%-20% 订单金额，或引导退货换货",
        permission_level=1,
        actions=["补偿优惠券", "部分退款", "退货换货"],
    ),
    AfterSaleRule(
        rule_id="R007",
        category="退款",
        condition="订单超出保障期或权益已过期",
        description="超出保障期的售后申请需转人工审核，客服无权直接同意",
        permission_level=2,
        actions=["转人工审核", "拒绝并说明原因"],
    ),
    AfterSaleRule(
        rule_id="R008",
        category="审核",
        condition="用户历史售后次数较高或金额较大",
        description="高频售后或大额订单需高级客服复核，避免资损",
        permission_level=3,
        actions=["人工复核", "风险标记"],
    ),
]


TICKETS: list[Ticket] = [
    Ticket(
        ticket_id="T20260201001",
        order_id="SO20260101001",
        user_id="U10001",
        intent="商品破损",
        description="收到无线蓝牙耳机后，发现耳机仓外壳有明显破损，无法正常使用",
        user_claim="商品在运输途中损坏",
        requested_action="退款",
    ),
    Ticket(
        ticket_id="T20260203002",
        order_id="SO20260114002",
        user_id="U10002",
        intent="物流异常",
        description="智能手环已经三天没有物流更新，用户等待时间过长",
        user_claim="物流停滞，要求补偿",
        requested_action="补偿",
    ),
    Ticket(
        ticket_id="T20260205003",
        order_id="SO20260120003",
        user_id="U10003",
        intent="退款",
        description="充电宝使用一周后无法充电，希望退款",
        user_claim="商品存在质量问题",
        requested_action="退款",
    ),
    Ticket(
        ticket_id="T20260206004",
        order_id="SO20251228004",
        user_id="U10004",
        intent="退款",
        description="机械键盘已超过售后保障期，用户仍要求退款",
        user_claim="键盘按键失灵，要求全额退款",
        requested_action="退款",
    ),
    Ticket(
        ticket_id="T20260207005",
        order_id="SO20260201005",
        user_id="U10005",
        intent="退款",
        description="保温杯下单后长时间未发货，用户要求取消订单并退款",
        user_claim="未收到商品，要求退款",
        requested_action="仅退款",
    ),
    Ticket(
        ticket_id="T20260208006",
        order_id="SO20260110006",
        user_id="U10006",
        intent="换货",
        description="降噪耳机左耳无声音，属于质量问题，希望换新",
        user_claim="耳机质量问题",
        requested_action="换货",
    ),
]


# Fixed scenario time and mock verified evidence keep replay independent of today's date.
for _ticket in TICKETS:
    _ticket.opened_at = datetime.strptime(_ticket.ticket_id[1:9], "%Y%m%d").replace(tzinfo=timezone.utc)
TICKETS[0].verified_facts = ["damage_confirmed", "damage_high"]
TICKETS[2].verified_facts = ["quality_confirmed"]
TICKETS[5].verified_facts = ["quality_confirmed"]
RULES.append(AfterSaleRule(rule_id="R009", category="审核", condition="资格条件或必要证据不足",
    description="缺少已核验证据、时间异常或方案无法确定时，停止自动建议并转人工补充材料",
    actions=["人工复核", "转人工审核"], permission_level=1))


def get_orders() -> list[Order]:
    return ORDERS


def get_order(order_id: str) -> Order | None:
    return next((order for order in ORDERS if order.order_id == order_id), None)


def get_logistics(order_id: str | None = None) -> list[LogisticsInfo] | LogisticsInfo | None:
    if order_id is None:
        return LOGISTICS
    return next((item for item in LOGISTICS if item.order_id == order_id), None)


def get_rules(category: str | None = None) -> list[AfterSaleRule]:
    if category is None:
        return RULES
    return [rule for rule in RULES if rule.category == category]


def get_tickets() -> list[Ticket]:
    return TICKETS


def get_ticket(ticket_id: str) -> Ticket | None:
    return next((ticket for ticket in TICKETS if ticket.ticket_id == ticket_id), None)


def search_rule_documents() -> list[dict]:
    """把规则库和常见问答构造成 RAG 检索文档。"""
    documents = []
    for rule in RULES:
        documents.append(
            {
                "id": rule.rule_id,
                "title": f"{rule.category}规则：{rule.description[:20]}",
                "content": (
                    f"适用条件：{rule.condition}。规则说明：{rule.description}。"
                    f"可执行动作：{'、'.join(rule.actions)}。"
                    f"所需客服权限等级：{rule.permission_level}。"
                ),
                "category": rule.category,
            }
        )
    documents.extend(
        [
            {
                "id": "FAQ001",
                "title": "如何判断商品是否在售后保障期内",
                "content": "主要依据订单签收时间与售后政策期限。退款通常为签收后 7 天，退货为 15 天，质量问题换货为 30 天。超出保障期需转人工审核。",
                "category": "FAQ",
            },
            {
                "id": "FAQ002",
                "title": "物流停滞如何处理",
                "content": "若物流信息超过 48 小时未更新，客服可先安抚用户，再结合物流轨迹判断是否申请补偿。补偿金额通常为订单金额的 5%-10%，以优惠券形式发放。",
                "category": "FAQ",
            },
            {
                "id": "FAQ003",
                "title": "商品破损的处理流程",
                "content": "先核对签收时间、物流轨迹与破损描述，再依据破损程度提供部分退款、补偿或退货换货方案。大额订单需高级客服复核。",
                "category": "FAQ",
            },
            {
                "id": "FAQ004",
                "title": "高频售后用户的处理要求",
                "content": "用户历史售后次数较多或订单金额较大时，系统应标记风险并转人工复核，客服不可直接执行大额退款或补偿。",
                "category": "FAQ",
            },
        ]
    )
    documents.extend([
        {"id": "CASE001", "title": "模拟案例：物流停滞补偿", "category": "补偿",
         "source": "mock/history", "content": "模拟历史案例：订单已发货，物流超过 48 小时未更新。依据 R004 核验轨迹后计算 5%-10% 小额补偿；大额订单按 R008 转人工。案例不代表当前工单事实。"},
        {"id": "CASE002", "title": "模拟案例：超期退款转人工", "category": "退款",
         "source": "mock/history", "content": "模拟历史案例：退款申请超出售后保障期，依据 R007 停止直接同意退款，转人工核验。用户质量问题陈述需单独收集证据，不得据此绕过期限。"},
    ])
    return documents

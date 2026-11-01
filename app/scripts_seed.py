"""演示数据：四个互不相通的群、九个人、十来条知识，用来直观验证权限隔离。

设计原则（改动前先读一遍，有测试依赖这些约束）：
- u1001 张三只属于客服群、u2001 李四只属于研发群，这两个人被测试断言过，
  不要给他们加额外授权，否则 tests/test_acl.py 会红。
- 待审核文档里出现的措辞（如「定制款商品」）不要同时出现在已生效文档里，
  否则 test_pending_doc_not_searchable 会误判。
- 所有 id / conversationId 都用固定的假值，方便在 README 和演示脚本里直接引用。
"""
from __future__ import annotations

import sqlite3

from . import config, db, store

SUPPORT = "chat_support"
DEV = "chat_dev"
PRODUCT = "chat_product"
EXT = "chat_ext"

CID_SUPPORT = "cidSupportAAAA"
CID_DEV = "cidDevBBBB"
CID_PRODUCT = "cidProductCCCC"
CID_EXT = "cidExtDDDD"

# (知识域, 标题, 正文, 标签)
DEMO_DOCS = [
    (
        SUPPORT,
        "七天无理由退货适用范围",
        "除生鲜、定制类、已拆封的软件授权卡外，签收后 7 天内可无理由退货；"
        "商品需保持吊牌完整、不影响二次销售。跨境商品不适用无理由退货。",
        "退货,售后,政策",
    ),
    (
        SUPPORT,
        "发票开具与重开规则",
        "订单完成后 30 天内可在「我的订单-申请发票」开具电子普票；专票需提供完整开票资料，"
        "3 个工作日内开具。发票抬头填错可在原票红冲后重开，一个月内限一次。",
        "发票,财务,流程",
    ),
    (
        SUPPORT,
        "大促期间物流延迟口径",
        "大促期间（11.1-11.15、6.1-6.20）发货承诺调整为 72 小时内，"
        "客服统一口径为「仓库爆仓，预计 3-5 天发出」，不给具体时间承诺。",
        "物流,大促,客服话术",
    ),
    (
        SUPPORT,
        "价保与降价补差规则",
        "签收后 15 天内同商品降价可申请补差，需提供降价截图且商品未拆封；"
        "大促期间的限时秒杀、优惠券叠加产生的价差不补。每月最多申请 2 次。",
        "价保,售后,补差",
    ),
    (
        SUPPORT,
        "会员积分过期与补发",
        "积分自获得当年起 12 个月内有效，到期前 30 天短信提醒。"
        "过期积分不自动补发；确因系统故障导致未到账的，凭订单号在 7 天内人工补发一次。",
        "会员,积分,规则",
    ),
    (
        DEV,
        "生产发布窗口与审批",
        "生产环境发布窗口为每周二、周四 20:00-22:00；紧急修复需值班经理 + 技术负责人双签，"
        "并在发布后 24 小时内补齐变更记录。周五及节假日禁止发布。",
        "发布,变更,流程",
    ),
    (
        DEV,
        "P0 告警处理时序",
        "P0 告警：5 分钟内响应，15 分钟内拉群并指定指挥人，30 分钟内给出止损方案。"
        "止损优先于定位根因，恢复后 2 个工作日内输出复盘文档。",
        "告警,应急,SRE",
    ),
    (
        DEV,
        "数据库慢查询排查顺序",
        "先看慢日志 Top10 与执行计划，确认是否缺索引；再看连接池是否打满；"
        "最后才考虑加机器。禁止在业务高峰期做 DDL 变更。",
        "数据库,性能,排查",
    ),
    (
        DEV,
        "灰度发布与回滚标准",
        "灰度按 1% → 10% → 50% → 100% 四档放量，每档观察 30 分钟；"
        "错误率超基线 2 倍或 P99 超过 800ms 立即回滚。回滚脚本必须随发布单一起提交。",
        "发布,灰度,回滚",
    ),
    (
        PRODUCT,
        "需求评审准入标准",
        "提评审必须带：目标指标、影响范围、竞品参考、埋点方案、上线时间。"
        "缺任意一项的当次不排期，补齐后进下一轮。评审结论 2 个工作日内同步到本群。",
        "需求,评审,流程",
    ),
    (
        PRODUCT,
        "埋点命名与上报规范",
        "事件名用「模块_对象_动作」下划线小写，如 cart_coupon_click；"
        "禁止中文和驼峰。新增埋点需在数据字典登记，未登记的埋点不予验收。",
        "埋点,数据,规范",
    ),
    (
        EXT,
        "外部协作群沟通边界",
        "外部协作群只讨论已签约项目的进度与交付物，不讨论价格政策、内部排期与人员安排；"
        "涉及合同条款的问题一律转对接商务在正式邮件里确认。",
        "外部协作,合规,边界",
    ),
]

# (知识域, 标题, 正文, 标签, 来源群)
DEMO_PENDING = [
    (
        SUPPORT,
        "定制款与拆封软件卡的退货结论",
        "定制款商品、已拆封的软件授权卡均不适用七天无理由退货，"
        "原因是定制款无法二次销售、软件卡拆封即视为激活。",
        "退货,定制,例外",
        CID_SUPPORT,
    ),
    (
        DEV,
        "值班交接清单",
        "交接时必须同步：进行中的故障单、待观察的灰度、明天的发布计划、"
        "以及当前告警抑制项。交接记录贴在本群置顶。",
        "值班,交接,草稿",
        CID_DEV,
    ),
    (
        PRODUCT,
        "新版首页改版结论",
        "首页首屏从三栏改两栏，搜索框上移；灰度 10% 时点击率提升 6%，"
        "但停留时长下降，是否全量等下周数据再定。",
        "首页,改版,待确认",
        CID_PRODUCT,
    ),
    (
        EXT,
        "外部客户群问答纪要",
        "客户问交付时间，按合同节点 11 月中回复，具体排期由对接人同步；"
        "客户问价格，一律转商务在正式邮件里确认，群里不讨论价格与合同条款。",
        "外部协作,交付,口径",
        CID_EXT,
    ),
]

# (群ID, 知识域, 昵称, staffId)
DEMO_GROUPS = [
    (CID_SUPPORT, "客服支持群", SUPPORT),
    (CID_DEV, "研发值班群", DEV),
    (CID_PRODUCT, "产品需求群", PRODUCT),
    (CID_EXT, "外部客户协作群", EXT),
]

# (staffId, 姓名, 角色)
DEMO_USERS = [
    ("root", "超级管理员", "superadmin"),
    ("admin001", "知识库管理员", "admin"),
    ("u1001", "张三", "member"),
    ("u1002", "王五", "member"),
    ("u2001", "李四", "member"),
    ("u3001", "赵敏", "member"),
    ("u3002", "钱伟", "member"),
    ("client001", "陈经理（客户）", "guest"),
    # 不在任何群、也没有单独授权的人，用来演示「默认拒绝」
    ("u9999", "新同事-未入群", "member"),
]

# (群ID, staffId, 昵称)
DEMO_MEMBERS = [
    (CID_SUPPORT, "u1001", "张三"),
    (CID_SUPPORT, "u1002", "王五"),
    (CID_DEV, "u2001", "李四"),
    (CID_PRODUCT, "u3001", "赵敏"),
    (CID_PRODUCT, "u3002", "钱伟"),
    (CID_EXT, "client001", "陈经理（客户）"),
]

# (群ID, 知识域, [(staffId, 昵称, 内容)])
DEMO_MESSAGES = [
    (
        CID_SUPPORT,
        SUPPORT,
        [
            ("u1001", "张三", "问一下，客户买了定制款还能退吗？"),
            ("u1002", "王五", "定制款不行，明确写在不适用里"),
            ("u1001", "张三", "那拆封的软件卡呢"),
            ("u1002", "王五", "也不行，拆封就等于激活了"),
            ("u1001", "张三", "价保那个呢，签收 20 天了还能补吗"),
            ("u1002", "王五", "超过 15 天就不行了，别答应客户"),
        ],
    ),
    (
        CID_DEV,
        DEV,
        [
            ("u2001", "李四", "今天发布窗口几点的？"),
            ("u2001", "李四", "周四 20:00-22:00，周五和节假日不能发"),
            ("u2001", "李四", "灰度每档观察多久"),
            ("u2001", "李四", "30 分钟，错误率超基线 2 倍就回滚"),
        ],
    ),
    (
        CID_PRODUCT,
        PRODUCT,
        [
            ("u3001", "赵敏", "需求评审要带哪些材料来着"),
            ("u3002", "钱伟", "目标指标、影响范围、竞品参考、埋点方案、上线时间"),
            ("u3001", "赵敏", "缺一项是不是就不能排期"),
            ("u3002", "钱伟", "对，补齐后进下一轮"),
        ],
    ),
    (
        CID_EXT,
        EXT,
        [
            ("client001", "陈经理（客户）", "这个版本什么时候能交付？"),
            ("u3002", "钱伟", "按合同节点 11 月中，具体排期我同步给贵司对接人"),
            ("client001", "陈经理（客户）", "价格上还能再谈吗"),
            ("u3002", "钱伟", "价格和合同条款走商务邮件确认，群里不讨论"),
        ],
    ),
]

# 跨群授权：不在群里、但被单独开了权限的人
DEMO_GRANTS = [
    ("u4001", "运营-周敏", SUPPORT, "临时支援大促客服"),
    ("u4001", "运营-周敏", PRODUCT, "跟首页改版项目"),
]


def _wipe(conn: sqlite3.Connection) -> None:
    for table in (
        "chunks_fts",
        "chunks",
        "docs",
        "messages",
        "group_members",
        "grants",
        "groups",
        "users",
        "scopes",
        "audit_log",
    ):
        conn.execute("DELETE FROM %s" % table)
    conn.commit()


def seed_demo(conn: sqlite3.Connection, force: bool = False, force_real: bool = False) -> bool:
    existing = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    if existing and not force:
        print("已有数据，跳过。需要重置请加 --force")
        return False

    if force:
        real = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE source <> 'demo'"
        ).fetchone()[0]
        if real and not force_real:
            # 这一行是为了防止"刷新演示数据"把真实群沉淀的知识一起删掉，
            # 之前就踩过：demo --force 把 DWS 拉回来的真实群消息清空了。
            print(
                "库里有 %d 条非演示数据（真实群沉淀的），--force 会把它们一起删掉。" % real
            )
            print("确认要清就加 --force-real；只想留真实数据请用 prune --keep <知识域>。")
            return False
        _wipe(conn)

    for cid, title, scope in DEMO_GROUPS:
        store.bind_group(conn, cid, title, scope)

    for staff_id, nick, role in DEMO_USERS:
        store.upsert_user(conn, staff_id, nick, role)

    for cid, staff_id, nick in DEMO_MEMBERS:
        store.touch_member(conn, cid, staff_id, nick)

    for staff_id, nick, scope, note in DEMO_GRANTS:
        store.upsert_user(conn, staff_id, nick, "member")
        store.add_grant(conn, staff_id, scope, can_read=True, note=note)

    for scope, title, content, tags in DEMO_DOCS:
        store.create_doc(
            conn,
            scope_key=scope,
            title=title,
            content=content,
            tags=tags,
            status="approved",
            source="demo",
            created_by="demo",
        )

    now = db.now_ms()
    for cid, scope, msgs in DEMO_MESSAGES:
        for i, (staff_id, nick, content) in enumerate(msgs):
            store.record_message(
                conn,
                conversation_id=cid,
                scope_key=scope,
                content=content,
                sender_id=staff_id,
                sender_nick=nick,
                msg_id="%s-%d" % (cid, i),
                created_at=now - (len(msgs) - i) * 60000,
            )

    # 关了审核就直接生效；开着审核才进「待审核」，演示什么状态跟着配置走
    pending_status = "approved" if config.auto_approve() else "pending"

    for scope, title, content, tags, cid in DEMO_PENDING:
        doc_id = store.create_doc(
            conn,
            scope_key=scope,
            title=title,
            content=content,
            tags=tags,
            status=pending_status,
            source="distill",
            source_conversation_id=cid,
            created_by="demo",
        )
        # 把这些消息标记成"已提炼"，否则定时沉淀会把同一批聊天再产一遍，
        # 演示数据里就会出现两份几乎一样的知识。
        pending = store.pending_messages(conn, scope)
        store.mark_messages_used(conn, [r["id"] for r in pending], doc_id)

    n_pending = len(DEMO_PENDING) if pending_status == "pending" else 0

    # 审计页留几条真实形状的记录，否则演示时那页是空的
    db.audit(
        conn,
        "seed.demo",
        actor="demo",
        detail="%d scopes / %d docs / %d pending"
        % (len(DEMO_GROUPS), len(DEMO_DOCS) + len(DEMO_PENDING), n_pending),
    )
    db.audit(conn, "ask", actor="u2001", target=SUPPORT, detail="q=发票怎么开 hits=0", allowed=False)
    db.audit(conn, "ask", actor="u1001", target=SUPPORT, detail="q=定制款还能退吗 hits=2")
    db.audit(conn, "ask", actor="client001", target=EXT, detail="q=什么时候交付 hits=1")
    db.audit(
        conn, "grant.add", actor="admin001", target="u4001@%s" % PRODUCT, detail="跟首页改版项目"
    )

    print(
        "已写入演示数据：%d 个群 / %d 个知识域、%d 篇知识（%d 待审核）、%d 条群消息、%d 条授权、%d 个用户"
        % (
            len(DEMO_GROUPS),
            len(DEMO_GROUPS),
            len(DEMO_DOCS) + len(DEMO_PENDING),
            n_pending,
            sum(len(m) for _, _, m in DEMO_MESSAGES),
            len(DEMO_GRANTS),
            len(DEMO_USERS) + len({g[0] for g in DEMO_GRANTS}),
        )
    )
    if not n_pending:
        print("当前 KNOWLEDGE_AUTO_APPROVE=true：知识不进待审核，直接生效。")
    return True

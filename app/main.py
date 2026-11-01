"""命令行入口。

    python -m app.main serve              # 起管理后台（默认 8000 端口）
    python -m app.main bot                # 起钉钉 Stream 机器人
    python -m app.main demo               # 灌入演示数据，先看效果
    python -m app.main import 文件.json   # 导入历史聊天记录
    python -m app.main distill            # 立即把待处理消息提炼成知识
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, db, distiller, ingest
from .ingest import file_import


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_serve(args) -> int:
    from . import api

    conn = db.get_db()
    db.init_db(conn)
    conn.close()

    if args.with_scheduler:
        threading.Thread(target=_scheduler_loop, daemon=True).start()

    api.serve(host=args.host, aclport=args.port)
    return 0


def _scheduler_loop() -> None:
    interval = max(60, args_interval())
    logging.info("定时沉淀已开启，每 %d 秒检查一次", interval)
    # 启动先跑一轮：服务重启后不该干等一个完整周期，把积压的消息先消化掉
    first = True
    while True:
        if not first:
            time.sleep(interval)
        first = False
        try:
            conn = db.get_db()
            results = distiller.distill_all(conn, actor="scheduler")
            created = sum(r.get("created", 0) for r in results if isinstance(r, dict))
            if created:
                logging.info("定时沉淀完成，新增 %d 条知识", created)
            conn.close()
        except Exception:  # noqa: BLE001
            logging.exception("定时沉淀失败")


def args_interval() -> int:
    return config.distill_interval_minutes() * 60


def cmd_bot(args) -> int:
    from .ingest import stream_bot

    try:
        stream_bot.build_client()
    except RuntimeError as e:
        print("启动失败：%s" % e, file=sys.stderr)
        return 2
    stream_bot.run_forever()
    return 0


def cmd_import(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print("文件不存在：%s" % path, file=sys.stderr)
        return 2
    items = file_import.load_file(path, conversation_id=args.conversation_id, title=args.title)
    if not items:
        print("没有解析出消息，检查一下文件格式。", file=sys.stderr)
        return 1
    conn = db.get_db()
    result = ingest.ingest_batch(conn, items)
    print("导入完成：%s" % result)
    if args.distill:
        scopes = {it["conversation_id"] for it in items}
        for key in scopes:
            from .ingest.normalizer import ensure_group

            scope_key = ensure_group(conn, key, args.title)
            print("提炼 %s → %s" % (scope_key, distiller.distill_scope(conn, scope_key, actor="import")))
    conn.close()
    return 0


def cmd_distill(args) -> int:
    conn = db.get_db()
    if args.scope:
        print(distiller.distill_scope(conn, args.scope, actor="cli"))
    else:
        for r in distiller.distill_all(conn, actor="cli"):
            print(r)
    conn.close()
    return 0


def cmd_demo(args) -> int:
    from .scripts_seed import seed_demo

    conn = db.get_db()
    ok = seed_demo(conn, force=args.force, force_real=getattr(args, "force_real", False))
    conn.close()
    if not ok:
        return 1
    print("演示数据已就绪。运行 `python -m app.main serve` 后打开 http://127.0.0.1:%d" % config.port())
    return 0


def cmd_ask(args) -> int:
    """命令行问答。不传问题则进入连续对话模式。"""
    from . import rag

    conn = db.get_db()
    staff = args.staff

    def one(question: str) -> None:
        answer = rag.ask(
            conn, staff_id=staff, question=question,
            conversation_id=args.chat or None, nickname=args.nickname or "",
        )
        print()
        if answer.refused:
            print("🚫", answer.text)
        else:
            print(answer.text)
        if answer.citations:
            print("\n（模式：%s｜检索域：%s）" % (answer.mode, "、".join(answer.scopes)))
        print()

    if args.question:
        one(args.question)
        conn.close()
        return 0

    print("以 %s 的身份提问（输入 exit 退出）" % staff)
    while True:
        try:
            question = input("问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question in {"exit", "quit", ":q"}:
            break
        if question:
            one(question)
    conn.close()
    return 0


# ---------------------------------------------------------------- DWS

def cmd_dws_check(args) -> int:
    from .ingest import dws

    print("=== DWS 自检 ===")
    path = shutil.which(dws.dws_bin())
    if not path:
        print("✗ 没找到 dws 命令")
        print("  安装：npm install -g dingtalk-workspace-cli")
        print("     或 brew install dingtalk-workspace-cli")
        print("  装好后如果还找不到，用 DWS_BIN=/绝对路径/dws 指定")
        return 2
    print("✓ 命令路径：%s" % path)
    print("  版本：%s" % dws.version())

    status = dws.auth_status()
    if not status.get("ok"):
        print("✗ 登录状态检查失败：%s" % status.get("error"))
        print("  执行：dws auth login")
        return 1
    print("✓ 登录状态：%s" % json.dumps(status.get("raw"), ensure_ascii=False)[:300])

    groups = dws.search_groups("")
    print("✓ 搜到群 %d 个" % len(groups))
    for g in groups[:10]:
        print("   - %s  %s" % (g["title"] or "(无标题)", g["conversation_id"]))
    print("\n当前事件类型：%s（改 DWS_EVENT_KIND）" % config.dws_event_kind())
    print("下一步：python -m app.main dws-dump   抓几条真实事件核对字段")
    return 0


def cmd_dws_dump(args) -> int:
    from .ingest import dws

    if not dws.available():
        print("没找到 dws 命令，先跑 dws-check", file=sys.stderr)
        return 2
    argv = [dws.dws_bin()] + dws.event_argv(args.kind, args.chat)
    print("监听中（最多 %d 秒 / %d 条）… 去钉钉群里发几句话" % (args.seconds, args.n))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = []
    deadline = time.time() + args.seconds
    try:
        while len(lines) < args.n and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.2)
                continue
            line = line.strip()
            if line:
                lines.append(line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not lines:
        print("没收到事件。检查：是否已 dws auth login / 群里有没有新消息 / DWS_EVENT_KIND 是否正确")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n原始事件已存到 %s（共 %d 条）\n" % (out, len(lines)))
    ok = 0
    for i, line in enumerate(lines, 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        norm = dws.normalize_event(raw)
        if norm:
            ok += 1
            print("[%d] ✓ %s | %s | %s" % (
                i, norm["conversation_title"] or norm["conversation_id"],
                norm["sender_nick"] or norm["sender_id"], norm["content"][:60]))
        else:
            print("[%d] ✗ 解析不出消息，原始键：%s" % (i, list(raw.keys())[:10]))
    print("\n解析成功 %d/%d 条。" % (ok, len(lines)))
    if ok < len(lines):
        print("有解析失败的事件，把 %s 里那几条的字段名发我，我调映射表。" % out)
    return 0


def cmd_dws_listen(args) -> int:
    from .ingest import dws

    if not dws.available():
        print("没找到 dws 命令，先跑 dws-check", file=sys.stderr)
        return 2
    conn = db.get_db()
    counter = {"seen": 0, "saved": 0}

    # 事件体里通常没有群名（只有 conversation_id），不补的话新群在后台会显示成一串 ID。
    # 但拉群列表首次调用要走鉴权，可能花几十秒——不能让订阅等着它，
    # 否则这段时间群里说的话就漏了。所以先订阅，群名放后台线程里补。
    titles = {}
    if args.kind in {"group_all", "group", "at"}:

        def load_titles() -> None:
            try:
                for g in dws.list_groups(limit=200):
                    titles[g["conversation_id"]] = g["title"]
                logging.info("已缓存 %d 个群名", len(titles))
            except Exception as exc:  # noqa: BLE001 —— 拿不到群名不影响收消息
                logging.warning("拉取群列表失败，不影响消息入库：%s", exc)

        threading.Thread(target=load_titles, daemon=True).start()

    def on_event(event):
        norm = dws.normalize_event(event)
        if norm is None:
            return
        if not norm.get("conversation_title"):
            norm["conversation_title"] = titles.get(norm["conversation_id"], "")
        counter["seen"] += 1
        result = ingest.ingest_message(conn, **norm)
        if result.get("ok") and result.get("is_new"):
            counter["saved"] += 1
            if counter["saved"] % 20 == 0:
                logging.info("已入库 %d 条群消息", counter["saved"])

    listener = dws.EventListener(kind=args.kind, chat_id=args.chat, on_event=on_event)
    print("DWS 事件监听已启动（%s）。Ctrl+C 停止。" % args.kind)
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。本次识别 %d 条，入库 %d 条。" % (counter["seen"], counter["saved"]))
        listener.stop()
    return 0


def cmd_dws_sync(args) -> int:
    from . import store
    from .ingest import archive_poller, dws

    conn = db.get_db()
    if args.chat:
        title = args.chat
        try:
            for g in dws.list_groups(limit=100):
                if g["conversation_id"] == args.chat:
                    title = g["title"] or title
                    break
        except Exception:  # noqa: BLE001
            pass
        targets = [{"conversation_id": args.chat, "title": title}]
    else:
        targets = store.list_groups(conn)
    if not targets:
        print("没有已绑定的群。先用 dws-check 拿到群 ID，再在后台或命令行绑定。")
        return 1

    since = int(time.time() * 1000) - args.days * 86400 * 1000 if args.days else 0
    total = 0
    for t in targets:
        result = archive_poller.pull(
            conn, t["conversation_id"], since_ms=since, title=t.get("title") or ""
        )
        total += result["inserted"]
        print("%-20s 新增 %d 条" % (t.get("title") or t["conversation_id"], result["inserted"]))
    print("合计新增 %d 条消息" % total)
    if args.distill:
        for r in distiller.distill_all(conn, actor="dws-sync"):
            if r.get("created"):
                print("沉淀 %s → %d 条知识" % (r["scope"], r["created"]))
    conn.close()
    return 0


def cmd_reindex(args) -> int:
    from . import store

    conn = db.get_db()
    try:
        n = store.reindex_all(conn)
    finally:
        conn.close()
    print("已重建 %d 个切片索引" % n)
    return 0


def cmd_prune(args) -> int:
    """只保留指定知识域，其余全部删掉（群、文档、切片、消息、授权、用户、审计）。"""
    keep = {s.strip() for s in (args.keep or "").split(",") if s.strip()}
    if not keep:
        print("必须用 --keep 指定要保留的知识域，例如 --keep chat_demand")
        return 2

    conn = db.get_db()
    try:
        alive = {r["key"] for r in conn.execute("SELECT key FROM scopes")}
        missing = keep - alive
        if missing:
            print("这些知识域不存在：%s" % ", ".join(sorted(missing)))
            return 2
        drop = alive - keep
        keep_cids = {
            r["conversation_id"]
            for r in conn.execute("SELECT conversation_id, scope_key FROM groups")
            if r["scope_key"] in keep
        }

        def count(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]

        plan = []
        if drop:
            ph = ",".join("?" * len(drop))
            args_drop = list(drop)
            plan.append(("消息", count("SELECT COUNT(*) FROM messages WHERE scope_key IN (%s)" % ph, args_drop)))
            plan.append(("知识文档", count("SELECT COUNT(*) FROM docs WHERE scope_key IN (%s)" % ph, args_drop)))
            plan.append(("检索切片", count("SELECT COUNT(*) FROM chunks WHERE scope_key IN (%s)" % ph, args_drop)))
            plan.append(("群/知识域", count("SELECT COUNT(*) FROM groups WHERE scope_key IN (%s)" % ph, args_drop)))
            plan.append(("授权", count("SELECT COUNT(*) FROM grants WHERE scope_key IN (%s)" % ph, args_drop)))

        print("保留知识域：%s" % ", ".join(sorted(keep)))
        print("将删除知识域：%s" % (", ".join(sorted(drop)) or "（无）"))
        for name, n in plan:
            print("  - %s %d 条" % (name, n))

        if not args.yes:
            print("\n这是预演。确认无误后加 --yes 真正执行。")
            return 0

        if drop:
            ph = ",".join("?" * len(drop))
            args_drop = list(drop)
            for r in conn.execute("SELECT id FROM chunks WHERE scope_key IN (%s)" % ph, args_drop).fetchall():
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["id"],))
            conn.execute("DELETE FROM chunks WHERE scope_key IN (%s)" % ph, args_drop)
            conn.execute("DELETE FROM docs WHERE scope_key IN (%s)" % ph, args_drop)
            conn.execute("DELETE FROM messages WHERE scope_key IN (%s)" % ph, args_drop)
            conn.execute("DELETE FROM grants WHERE scope_key IN (%s)" % ph, args_drop)
            conn.execute("DELETE FROM groups WHERE scope_key IN (%s)" % ph, args_drop)
            conn.execute("DELETE FROM scopes WHERE key IN (%s)" % ph, args_drop)
            if keep_cids:
                phc = ",".join("?" * len(keep_cids))
                conn.execute(
                    "DELETE FROM group_members WHERE conversation_id NOT IN (%s)" % phc,
                    list(keep_cids),
                )
            conn.execute("DELETE FROM audit_log")

        # 只留管理员和保留群的真实成员，其余用户（演示账号）清掉
        survivors = {r["staff_id"] for r in conn.execute(
            "SELECT staff_id FROM users WHERE role IN ('superadmin','admin')"
        )}
        if keep_cids:
            phc = ",".join("?" * len(keep_cids))
            survivors |= {
                r["staff_id"]
                for r in conn.execute(
                    "SELECT DISTINCT staff_id FROM group_members WHERE conversation_id IN (%s)" % phc,
                    list(keep_cids),
                )
            }
        removed_users = [
            r["staff_id"]
            for r in conn.execute("SELECT staff_id FROM users")
            if r["staff_id"] not in survivors
        ]
        for sid in removed_users:
            conn.execute("DELETE FROM users WHERE staff_id = ?", (sid,))
        for sid in survivors:
            row = conn.execute(
                "SELECT nickname FROM group_members WHERE staff_id = ? LIMIT 1", (sid,)
            ).fetchone()
            if row and row["nickname"]:
                conn.execute(
                    "UPDATE users SET nickname = ? WHERE staff_id = ? AND (nickname IS NULL OR nickname = '' OR nickname = staff_id)",
                    (row["nickname"], sid),
                )
        # 群里的人补登记成用户，否则问答页的身份下拉里看不到他们
        if keep_cids:
            phc = ",".join("?" * len(keep_cids))
            for r in conn.execute(
                "SELECT staff_id, MAX(nickname) AS nick FROM group_members WHERE conversation_id IN (%s) GROUP BY staff_id" % phc,
                list(keep_cids),
            ):
                if r["staff_id"] not in survivors:
                    conn.execute(
                        "INSERT INTO users (staff_id, nickname, role, status, created_at)"
                        " VALUES (?,?,'member','active',?)"
                        " ON CONFLICT(staff_id) DO NOTHING",
                        (r["staff_id"], r["nick"] or "", db.now_ms()),
                    )
        conn.commit()

        print("\n删除用户 %d 个：%s" % (len(removed_users), ", ".join(sorted(removed_users)) or "—"))
        print("剩余：%d 个知识域 / %d 群 / %d 消息 / %d 文档 / %d 用户"
              % (
                  count("SELECT COUNT(*) FROM scopes"),
                  count("SELECT COUNT(*) FROM groups"),
                  count("SELECT COUNT(*) FROM messages"),
                  count("SELECT COUNT(*) FROM docs"),
                  count("SELECT COUNT(*) FROM users"),
              ))
        return 0
    finally:
        conn.close()


def cmd_migrate_mysql(args) -> int:
    """把本地 SQLite 的数据整库搬到 MySQL（建库建表 + 全量复制）。"""
    import sqlite3 as _sqlite3

    from . import db_mysql

    cfg = config.mysql_config()
    if cfg is None:
        print("先在 .env 里配好 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE")
        return 2

    src_path = config.db_path()
    if not src_path.exists():
        print("找不到本地 SQLite：%s" % src_path)
        return 2

    print(
        "目标：mysql://%s@%s:%s/%s"
        % (cfg["user"], cfg["host"], cfg["port"], cfg["database"])
    )
    if not args.yes:
        print("这是预演，加 --yes 真正执行（会先清空目标库里的同名表）")
        return 0

    db_mysql.create_database(cfg)
    dst = db_mysql.connect(cfg)
    db_mysql.init_db(dst)

    src = _sqlite3.connect(str(src_path))
    src.row_factory = _sqlite3.Row
    report = db_mysql.migrate_from_sqlite(src, dst)
    src.close()
    dst.close()

    total = sum(report.values())
    for table, n in report.items():
        print("  %-14s %d 行" % (table, n))
    print("共迁移 %d 行。" % total)
    print("在 .env 里加 KB_BACKEND=mysql（以及 MYSQL_* 配置）后重启，服务就改用 MySQL 了。")
    return 0


def main(argv=None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="kb-bot", description="钉钉群知识机器人")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动管理后台")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=config.port())
    p_serve.add_argument("--with-scheduler", action="store_true", help="同时开启定时沉淀")
    p_serve.set_defaults(func=cmd_serve)

    p_bot = sub.add_parser("bot", help="启动钉钉 Stream 机器人")
    p_bot.set_defaults(func=cmd_bot)

    p_imp = sub.add_parser("import", help="导入聊天记录文件")
    p_imp.add_argument("file")
    p_imp.add_argument("--conversation-id", default="")
    p_imp.add_argument("--title", default="")
    p_imp.add_argument("--distill", action="store_true", help="导入后立即提炼")
    p_imp.set_defaults(func=cmd_import)

    p_dis = sub.add_parser("distill", help="把待处理消息提炼成知识")
    p_dis.add_argument("--scope", default="")
    p_dis.set_defaults(func=cmd_distill)

    p_demo = sub.add_parser("demo", help="写入演示数据")
    p_demo.add_argument("--force", action="store_true", help="清空后重建（仅当库里只有演示数据时）")
    p_demo.add_argument("--force-real", action="store_true", help="连真实群数据一起删，慎用")
    p_demo.set_defaults(func=cmd_demo)

    p_ask = sub.add_parser("ask", help="命令行提问（不传问题则连续对话）")
    p_ask.add_argument("question", nargs="?", default="", help="要问的问题")
    p_ask.add_argument("--staff", default="admin001", help="以谁的身份提问（决定能看到哪些知识）")
    p_ask.add_argument("--chat", default="", help="在哪个群的上下文里问（留空=私聊，可查全部可读域）")
    p_ask.add_argument("--nickname", default="")
    p_ask.set_defaults(func=cmd_ask)

    p_dwsc = sub.add_parser("dws-check", help="自检 dws：安装、登录、能搜到哪些群")
    p_dwsc.set_defaults(func=cmd_dws_check)

    p_dwsd = sub.add_parser("dws-dump", help="抓几条真实事件，核对字段映射")
    p_dwsd.add_argument("--kind", default=config.dws_event_kind())
    p_dwsd.add_argument("--chat", default="")
    p_dwsd.add_argument("--n", type=int, default=5)
    p_dwsd.add_argument("--seconds", type=int, default=60)
    p_dwsd.add_argument("--out", default="data/dws_sample.ndjson")
    p_dwsd.set_defaults(func=cmd_dws_dump)

    p_dwsl = sub.add_parser("dws-listen", help="实时监听群消息并入库")
    p_dwsl.add_argument("--kind", default=config.dws_event_kind(),
                        choices=["group_all", "group", "at", "o2o_all"])
    p_dwsl.add_argument("--chat", default="", help="kind=group 时指定 openConversationId")
    p_dwsl.set_defaults(func=cmd_dws_listen)

    p_dwss = sub.add_parser("dws-sync", help="拉取历史群消息")
    p_dwss.add_argument("--chat", default="", help="留空则同步所有已绑定的群")
    p_dwss.add_argument("--days", type=int, default=7, help="回溯天数，0 表示不限")
    p_dwss.add_argument("--distill", action="store_true", help="同步后立即提炼")
    p_dwss.set_defaults(func=cmd_dws_sync)

    p_rei = sub.add_parser("reindex", help="重建全部已生效文档的检索索引")
    p_rei.set_defaults(func=cmd_reindex)

    p_prune = sub.add_parser("prune", help="只保留指定知识域，其余数据全部删除")
    p_prune.add_argument("--keep", required=True, help="要保留的知识域，多个用逗号分隔")
    p_prune.add_argument("--yes", action="store_true", help="真正执行（不加只预演）")
    p_prune.set_defaults(func=cmd_prune)

    p_mig = sub.add_parser("migrate-mysql", help="把 SQLite 数据整库搬到 MySQL")
    p_mig.add_argument("--yes", action="store_true", help="真正执行（不加只预演）")
    p_mig.set_defaults(func=cmd_migrate_mysql)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["serve"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

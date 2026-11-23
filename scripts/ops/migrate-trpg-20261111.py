"""本机一次性迁移：默认只预览，--apply 创建待确认的暂停团；不联网、不发QQ消息。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from companion.config import load_config
from companion.models.sessions import ChatSessions
from companion.skills.base import SkillContext
from companion.trpg.controller import Controller
from companion.trpg.policy import protect, registry
from companion.trpg.store import Store, dump

CHANNEL = "group:826445390"
GM, PLAYER = "pe57b1620", "p8dc4334b"


def plan(root):
    cfg = load_config(root)
    folder = cfg.data_dir / "workspace/chats/group-826445390"
    saved = folder / "跑团存档/埃莉诺_1111_1554.md"
    public = folder / "KP准备/08_玩家无剧透简报.md"
    private = [p for p in (folder / "KP准备").glob("*.md") if p != public]
    private += [saved, folder / "疗养院的噩梦.pdf"]
    if not saved.is_file() or "HP5/10" not in saved.read_text("utf-8"):
        raise ValueError("原存档不匹配，请人工核对，不猜测覆盖")
    secret_events, fiction = [], []
    names = [p.name for p in private if p.name != "写入测试.md"]
    for logfile in (cfg.data_dir / "memory/events").glob("*.jsonl"):
        for line in logfile.read_text("utf-8").splitlines():
            e = json.loads(line)
            if e["channel"] != CHANNEL:
                continue
            t = e["ts"]
            serialized = json.dumps(e, ensure_ascii=False)
            if "2026-11-11T02:38" <= t < "2026-11-11T02:58" or any(
                name in serialized for name in names
            ):
                secret_events.append(e["id"])
            if "2026-11-11T06:20" <= t < "2026-11-11T07:56" and e["actor"].get("person_id") in {
                PLAYER,
                "self",
            }:
                fiction.append(e["id"])
    tasks = json.loads((cfg.data_dir / "run/tasks.json").read_text("utf-8"))["tasks"]
    secret_tasks = [
        r["id"] for r in tasks.values() if "KP准备" in dump(r) or "疗养院的噩梦" in dump(r)
    ]
    # 原PDF对应的页图/OCR缓存也进入受保护清单（只登记，不改动）。
    pdf = folder / "疗养院的噩梦.pdf"
    cache = (
        cfg.data_dir / "run/documents" / hashlib.sha256(b"pdf-v1\0" + pdf.read_bytes()).hexdigest()
    )
    cached = [p for p in cache.rglob("*") if p.is_file()]
    sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [*private, *cached]}
    return cfg, folder, saved, public, private, sources, secret_events, fiction, secret_tasks


def migrate(root, apply=False):
    cfg, folder, saved, public, private, sources, events, fiction, tasks = plan(root)
    report = {
        "apply": apply,
        "private_sources": len(sources),
        "masked_events": len(events),
        "fiction_events": len(fiction),
        "protected_tasks": len(tasks),
        "save_sha": sources[str(saved)],
        "status": "paused",
        "HP": 5,
        "SAN": 64,
    }
    if not apply:
        return report
    controller = Controller(cfg)
    with controller.store.connect() as conn:
        for row in conn.execute("SELECT * FROM campaigns WHERE channel=?", (CHANNEL,)):
            existing = Store.load(row)
            if existing["state"].get("migration", {}).get("save_sha") == report["save_sha"]:
                return {**report, "campaign": existing["id"], "already_migrated": True}
    backup = cfg.data_dir / "backups" / ("trpg-data-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True)
    for file in [
        registry(cfg.data_dir),
        cfg.data_dir / "run/chat-sessions.sqlite",
        controller.store.path,
    ]:
        if file.exists():
            target = backup / file.name
            if file.suffix == ".sqlite":
                with sqlite3.connect(file) as src, sqlite3.connect(target) as dest:
                    src.backup(dest)
            else:
                target.write_bytes(file.read_bytes())
    gm = SkillContext("qq", CHANNEL, GM, False, True)
    service = controller.service
    cid = service.execute(gm, "创建", {"name": "疗养院单人团 · 恢复草案"})["campaign"]
    service.execute(gm, "成员", {"person": PLAYER, "role": "player"})
    card_source = folder / "调查员/喵了个咪_埃莉诺里德_人物卡.md"
    card_text = card_source.read_text("utf-8")
    skills = {
        name.strip(): int(value)
        for name, value in re.findall(r"^\|([^|]+)\|(\d+)\|", card_text, re.M)
    }
    skills = {
        k: v for k, v in skills.items() if not re.search(r"STR|CON|SIZ|DEX|APP|INT|POW|EDU|幸运", k)
    }
    skills.update({"斗殴": 45, "斧": 15, "手枪": 20})
    items = {
        name: 1
        for name in [
            "相机及少量胶卷",
            "采访本",
            "铅笔",
            "钢笔",
            "怀表",
            "手帕",
            "小电筒",
            "普通绷带",
            "大衣和衣服",
            "消防斧",
            "锤子",
            "手锯",
            "绳索",
            "未点燃临时火把",
            "地下室钥匙圈",
            ".32左轮",
            "薄金属片",
        ]
    }
    items.update({"美元": 10, "砖块": 2, ".32子弹": 6})
    service.execute(
        gm,
        "建卡",
        {
            "person": PLAYER,
            "name": "埃莉诺·里德",
            "card": {
                "STR": 50,
                "CON": 55,
                "SIZ": 45,
                "DEX": 60,
                "INT": 60,
                "POW": 70,
                "EDU": 64,
                "APP": 35,
                "HP_MAX": 10,
                "HP": 5,
                "MP_MAX": 14,
                "MP": 14,
                "SAN": 64,
                "LUCK": 60,
                "skills": skills,
                "items": items,
                "conditions": ["重伤"],
            },
        },
    )
    description = (
        "地下室另一牢房区，埃莉诺站在锁闭栅栏门外。方才挥斧未命中，双方均未受伤。"
        "牢房内的人仍未回答；暂停后没有追加敌方行动。"
    )
    service.execute(
        gm,
        "场景",
        {
            "scene": {
                "id": "cell-block",
                "title": "地下室另一牢房区",
                "public": description,
                "actions": [
                    {
                        "id": "recap",
                        "title": "回顾当前可见情况",
                        "aliases": ["看看周围", "观察周围", "回顾现场"],
                        "success_text": description,
                    }
                ],
            }
        },
    )
    for fid, content in [
        ("saved-progress", "此前已找到工具和钥匙；浴室墙洞尚不能通过。原有已结算检定不重投。"),
        ("pause-point", description),
    ]:
        service.execute(
            gm, "事实", {"id": fid, "text": content, "source": saved.name, "public": True}
        )
    # 先按原范围导入，再封闭原路径及缓存；保留所有原文件。
    for source in private:
        if source.suffix == ".md" and source.name != "写入测试.md":
            controller.library.import_file(gm, cid, source, "kp")
    controller.library.import_file(gm, cid, public, "public")
    controller.library.import_file(gm, cid, folder / "跑团规则/COC7_完整正文.md", "public")
    with controller.store.connect(write=True) as conn:
        campaign = service.campaign(gm, cid, conn)
        campaign["status"] = "paused"
        state = campaign["state"]
        state["rulings"] = [
            {
                "name": "现行约定",
                "text": "单人适配；1925记者原卡；不启用特殊能力；恐怖描写收敛；真实骰且不自动重投",
                "source": saved.name,
                "public": True,
            }
        ]
        state.update(
            needs_confirmation=True,
            migration={
                "save_sha": report["save_sha"],
                "sources": sources,
                "uncertainties": [
                    "旧存档称HP5/10仍重伤；标准规则治疗到一半可解除，保持旧状态待KP确认",
                    "当前交战双方后续DEX轮次未确认，不自动重建战斗或追加NPC行动",
                    "物品数量中胶卷、绷带等按一个携带条目记录，未假定剩余消耗次数",
                    "只采用玩家已经看到的场景；新场景与模组特例须KP审查后采纳",
                ],
            },
        )
        _, card = service.pc(campaign, PLAYER)
        card["san_day_start"], card["san_loss_today"] = 70, 6
        card["wounds"] = [{"damage": 6, "first_aid": True, "healed": False}]
        state["members"][PLAYER]["auto_roll"] = True  # 原存档明确接受KP代骰；仍需逐次风险确认。
        Store.save(conn, campaign)
        Store.event(
            conn,
            cid,
            GM,
            "恢复迁移",
            "暂停进度已恢复为待确认草案，未掷骰或推进剧情。",
            report,
            "kp",
        )
        conn.execute("UPDATE outbox SET state='migration_local' WHERE campaign=?", (cid,))
    protect(
        registry(cfg.data_dir), [Path(p) for p in sources], sources.values(), events, tasks, fiction
    )
    sessions = ChatSessions(cfg.data_dir / "run/chat-sessions.sqlite")
    retired = len(sessions.retire("trpg-privacy-migration"))
    sessions.close()
    receipt = {**report, "campaign": cid, "retired_chat_contexts": retired, "backup": str(backup)}
    (backup / "migration-receipt.json").write_text(dump(receipt), "utf-8")
    controller.close()
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args()
    print(dump(migrate(options.root.resolve(), options.apply)))

"""为已暂停团装入审核后的模组并授权专用模型；默认预览，不发送QQ消息。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from companion.config import load_config
from companion.skills.base import SkillContext
from companion.trpg import rules
from companion.trpg.controller import Controller
from companion.trpg.policy import protect, registry
from companion.trpg.scenes import validate_module
from companion.trpg.store import Store, dump


def enable(root, cid, pack_path, *, apply=False, consent="", authorized=False, derivatives=()):
    cfg = load_config(root)
    db = cfg.data_dir / "trpg/state.sqlite"
    raw = pack_path.read_bytes()
    pack = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError("没有指定的团")
        existing = Store.load(row)
        documents = [
            dict(r)
            for r in conn.execute(
                "SELECT DISTINCT source,sha FROM documents WHERE campaign=?", (cid,)
            )
        ]
    if existing["state"].get("module", {}).get("manifest_sha") == digest:
        return {"campaign": cid, "already_installed": True}
    if existing["status"] != "paused":
        raise ValueError("安装前须暂停，避免覆盖正在进行的行动")
    if pack.get("schema") != 1 or pack.get("source_sha") not in {r["sha"] for r in documents}:
        raise ValueError("模组来源版本不匹配，拒绝安装")
    for document in documents:
        if document["sha"] == pack["source_sha"]:
            if (
                hashlib.sha256(Path(document["source"]).read_bytes()).hexdigest()
                != pack["source_sha"]
            ):
                raise ValueError("来源文件已经改变")
    draft = validate_module(pack, existing["state"])
    if existing["state"]["scene"] not in {s["id"] for s in draft["scenes"]}:
        raise ValueError("审核包未覆盖当前地点，不自动传送角色")
    npc_data = pack.get("npcs", {})
    if not isinstance(npc_data, dict) or len(npc_data) > 30:
        raise ValueError("NPC资料格式不对")
    for profile in npc_data.values():
        rules.character(profile["card"], npc=True)
        rules.Expression(profile["damage"])
        rules.Expression(profile.get("db", "0"))
        rules.integer(profile.get("attacks", 1), 1, 2, "每轮攻击次数")
        rules.integer(profile.get("armor", 0), 0, 20, "护甲")
    report = {
        "campaign": cid,
        "apply": apply,
        "scenes": len(draft["scenes"]),
        "facts": len(draft["facts"]),
        "npcs": len(npc_data),
        "manifest_sha": digest,
        "source_sha": pack["source_sha"],
        "retains_status": "paused",
    }
    if not apply:
        return report
    if not authorized or not consent.strip():
        raise ValueError("必须明确授权专用主持模型读取该模组，并记录授权依据")
    backup = cfg.data_dir / "backups" / ("ai-host-data-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True)
    for path in [
        db,
        registry(cfg.data_dir),
        cfg.data_dir / "run/trpg-sessions.sqlite",
        cfg.data_dir / "run/trpg-kp-sessions.sqlite",
    ]:
        if not path.exists():
            continue
        target = backup / path.name
        if path.suffix == ".sqlite":
            with sqlite3.connect(path) as src, sqlite3.connect(target) as dst:
                src.backup(dst)
        else:
            target.write_bytes(path.read_bytes())
    c = Controller(cfg)
    ctx = SkillContext(existing["body"], existing["channel"], existing["kp"], False, True)
    target_pack = cfg.data_dir / "trpg/modules" / (cid + "-" + digest[:12] + ".json")
    target_pack.parent.mkdir(parents=True, exist_ok=True)
    if target_pack.exists() and target_pack.read_bytes() != raw:
        raise ValueError("目标包内容不符，不覆盖")
    target_pack.write_bytes(raw)
    approved_names = {"疗养院的噩梦_完整正文.md", "09_核验更正与现行入口.md", "埃莉诺_1111_1554.md"}
    hashes = sorted(
        {
            d["sha"]
            for d in documents
            if Path(d["source"]).name in approved_names or d["sha"] == pack["source_sha"]
        }
    )
    protect(
        registry(cfg.data_dir),
        [target_pack, pack_path.resolve(), *derivatives],
        [digest, *[hashlib.sha256(p.read_bytes()).hexdigest() for p in derivatives]],
    )
    with c.store.connect(write=True) as conn:
        campaign = c.service.campaign(ctx, cid, conn)
        if campaign["version"] != existing["version"]:
            raise ValueError("团状态在准备期间发生变化，未安装")
        state = campaign["state"]
        mappings = {name: "module:" + cid + ":" + name for name in npc_data}
        profiles = {}
        for name, data in npc_data.items():
            key = mappings[name]
            if key not in state["characters"]:
                card = rules.character(data["card"], npc=True)
                state["characters"][key] = {**card, "name": data["name"], "owner": "", "npc": True}
            profiles[key] = {k: v for k, v in data.items() if k != "card"}
        for scene in draft["scenes"]:
            scene["npcs"] = [mappings[name] for name in scene.get("npcs", [])]
            # 当前现场仍以已保存公开叙述为基准，变化由条件描述补充。
            if scene["id"] == state["scene"]:
                scene["public"] = state["scenes"][state["scene"]]["public"]
            state["scenes"][scene["id"]] = scene
        for fact in draft["facts"]:
            state["facts"].setdefault(fact["id"], fact)
        for key, value in pack.get("flags", {}).items():
            state["flags"].setdefault(key, value)
        state["module"] = {
            "name": pack["name"],
            "manifest_sha": digest,
            "source_sha": pack["source_sha"],
            "npcs": profiles,
            "weapons": pack.get("weapons", {}),
            "package": str(target_pack),
            "darkness_clock": bool(pack.get("darkness_clock")),
            "retry_flags": pack.get("retry_flags", ["power_on"]),
        }
        state.setdefault("rulings", []).extend(
            {
                "name": "AI主持接续采用约定",
                "text": line,
                "source": "审核后的模组接续包",
                "public": False,
            }
            for line in pack.get("adoptions", [])
        )
        state["knowledge_version"] = state.get("knowledge_version", 0) + 1
        c.service._mutate(
            conn,
            campaign,
            ctx,
            "主持授权",
            {"enabled": True, "allow_private_model": True, "sources": hashes, "consent": consent},
        )
        Store.save(conn, campaign)
        Store.event(
            conn,
            cid,
            ctx.person_id,
            "AI主持准备",
            "已安装来源锁定的本团场景与NPC，进度保持暂停。",
            {**report, "consent": consent},
            "kp",
        )
    with c.store.connect(write=True) as conn:
        conn.execute(
            "UPDATE outbox SET state='installation_local' WHERE campaign=? AND state='pending'",
            (cid,),
        )
    result = {
        **report,
        "backup": str(backup),
        "private_sources": len(hashes),
        "ai_host_enabled": True,
    }
    (backup / "receipt.json").write_text(dump(result), "utf-8")
    c.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorize-private-model", action="store_true")
    parser.add_argument("--consent", default="")
    parser.add_argument("--private-derivative", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(
        dump(
            enable(
                args.root.resolve(),
                args.campaign,
                args.pack,
                apply=args.apply,
                consent=args.consent,
                authorized=args.authorize_private_model,
                derivatives=args.private_derivative,
            )
        )
    )

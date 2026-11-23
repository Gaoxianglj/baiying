from __future__ import annotations

import hashlib
import re

from companion.skills.base import SkillError
from companion.trpg.policy import protect, registry
from companion.trpg.store import Store, dump, uid


class Library:
    def __init__(self, service, access, data_dir):
        self.service, self.access, self.data_dir = service, access, data_dir

    def import_file(self, ctx, cid, path, visibility="kp", event_ids=()):
        if visibility not in {"kp", "public", "party", "character"}:
            raise SkillError("资料可见性不合法")
        campaign = self.service.campaign(ctx, cid)
        self.service.kp(campaign, ctx)
        source, member = self.access.locate(str(path), ctx)
        if member:
            raise SkillError("压缩包内资料先转存成MD文件后再导入")
        if not source.is_file() or source.stat().st_size > 25 * 1024 * 1024:
            raise SkillError("来源文件不存在或超过25MB")
        # 只按普通文件访问范围导入；不能借KP身份读取密钥、记忆或其他群文件。
        # 已登记的私有来源允许同一团再次复用，由库内记录和指纹校验，不重开普通读取权限。
        with self.service.store.connect() as conn:
            old = conn.execute(
                "SELECT id,sha,visibility FROM documents WHERE campaign=? AND source=? LIMIT 1",
                (cid, str(source)),
            ).fetchone()
        if not old:
            self.access.check_read(source, ctx)
        raw = source.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if old:
            if old["sha"] != sha or old["visibility"] != visibility:
                raise SkillError(
                    "来源已变更或试图改变可见性，请显式导入新版本，不能覆盖或解密旧资料"
                )
            return {
                "text": "相同来源和版本已经入库，复用索引。",
                "changed": False,
                "visibility": "kp",
            }
        if source.suffix.lower() == ".pdf":
            from companion.skills.documents import Documents

            pages = Documents(self.data_dir / "run" / "documents").pages(raw)
            numbered = list(enumerate(pages, 1))
        elif source.suffix.lower() in {".md", ".txt", ".xlsx"}:
            from companion.skills.reader import extract_text

            content = extract_text(source.name, raw, 25 * 1024 * 1024)
            parts = re.split(r"(?m)^## PDF 第 (\d+) 页[^\n]*\n", content)
            numbered = (
                [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]
                if len(parts) > 1
                else list(
                    enumerate([content[i : i + 2400] for i in range(0, len(content), 2400)], 1)
                )
            )
            pages = [value for _, value in numbered]
        else:
            raise SkillError("资料库仅接收PDF/MD/TXT，不执行文档内指令")
        if not any(page.strip() for page in pages):
            raise SkillError("文档没有文字层，请先明确授权逐页识别并转存文本，避免自动OCR消耗额度")
        if visibility != "public":
            # 普通路径先封闭，再提交私有索引；任何失败都不能把秘密当公开资料返回。
            protect(registry(self.data_dir), [source], [sha], event_ids)
        with self.service.store.connect(write=True) as conn:
            for page, content in numbered:
                for offset in range(0, len(content), 2400):
                    conn.execute(
                        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            uid(),
                            cid,
                            visibility,
                            ctx.person_id,
                            source.name,
                            sha,
                            page,
                            content[offset : offset + 2400],
                            str(source),
                        ),
                    )
            Store.event(
                conn,
                cid,
                ctx.person_id,
                "资料导入",
                "资料已按版本和可见性入库。",
                {"sha": sha, "visibility": visibility, "pages": len(pages)},
                "kp",
            )
        return {
            "text": f"资料已入库：{len(pages)}页/段，版本{sha[:12]}，可见性{visibility}。",
            "changed": True,
            "visibility": "kp",
            "campaign": cid,
        }

    def search(self, ctx, cid, query, *, private=False, limit=3, max_chars=1600):
        campaign = self.service.campaign(ctx, cid)
        if private:
            self.service.kp(campaign, ctx)
            if not ctx.private:
                raise SkillError("KP原文只能在私聊或本地查看，群里不返回秘密片段")
        words = [w.lower() for w in query.split() if w][:8]
        if not words or len(query) > 120:
            raise SkillError("请给出简短检索词")
        with self.service.store.connect() as conn:
            rows = conn.execute("SELECT * FROM documents WHERE campaign=?", (cid,)).fetchall()
        role = self.service.role(campaign, ctx)
        ranked = []
        for row in rows:
            visible = (
                row["visibility"] == "public"
                or (ctx.private and role in {"kp", "player"} and row["visibility"] == "party")
                or (
                    ctx.private
                    and row["visibility"] == "character"
                    and row["owner"] == ctx.person_id
                )
                or private
            )
            if not visible:
                continue
            score = sum(row["text"].lower().count(word) for word in words)
            if score:
                ranked.append((score, dict(row)))
        fragments, remaining = [], max_chars
        for _, row in sorted(ranked, key=lambda item: -item[0])[:limit]:
            at = min(
                (row["text"].lower().find(w) for w in words if w in row["text"].lower()), default=0
            )
            excerpt = row["text"][max(0, at - 120) : at + 600][:remaining]
            remaining -= len(excerpt)
            fragments.append(
                {
                    "id": row["id"],
                    "source": row["name"],
                    "sha": row["sha"][:12],
                    "pdf_page_or_chunk": row["page"],
                    "printed_page": None,
                    "text": excerpt,
                    "verified": False,
                }
            )
            if remaining <= 0:
                break
        return {
            "text": dump(fragments) if fragments else "当前权限可见资料中未找到，请勿编造出处。",
            "fragments": fragments,
            "visibility": "kp" if private else "actor",
            "changed": False,
        }

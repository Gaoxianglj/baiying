"""敏感资料清单：普通文件/历史/记忆路径均不授予KP秘密访问权。"""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile
from uuid import uuid4

from companion.skills.base import SkillError

_LOCK = threading.RLock()
_CACHE = {}
_HASHES = {}


def registry(data_dir):
    return data_dir / "run" / "trpg-protected.json"


def load(path):
    if not path.exists():
        return {"paths": [], "hashes": [], "events": []}
    stamp = path.stat().st_mtime_ns
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            value = json.loads(path.read_text("utf-8"))
            if not all(isinstance(value.get(k), list) for k in ("paths", "hashes", "events")):
                raise ValueError("invalid registry")
        except (ValueError, OSError) as error:
            raise SkillError("跑团资料权限清单损坏，暂时拒绝读取以免误公开") from error
        _CACHE[str(path)] = stamp, value
        return value


def protect(path, sources=(), hashes=(), events=(), tasks=(), fiction=()):
    with _LOCK:
        data = dict(load(path))
        data["paths"] = sorted(set(data["paths"]) | {str(p.resolve()).casefold() for p in sources})
        data["hashes"] = sorted(set(data["hashes"]) | set(hashes))
        data["events"] = sorted(set(data["events"]) | set(events))
        data["tasks"] = sorted(set(data.get("tasks", [])) | set(tasks))
        data["fiction"] = sorted(set(data.get("fiction", [])) | set(fiction))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        temporary.replace(path)
        _CACHE.pop(str(path), None)


def check_payload(data, policy):
    info = load(policy)
    if info["hashes"] and hashlib.sha256(data).hexdigest() in info["hashes"]:
        raise SkillError("这是KP受保护资料，请使用本团有权限的资料接口")


def check_path(path, policy):
    info = load(policy)
    resolved = path.resolve()
    if str(resolved).casefold() in info["paths"]:
        raise SkillError("这是KP受保护资料，普通文件工具不能读或发")
    if not info["hashes"] or not resolved.is_file():
        return
    stat = resolved.stat()
    key = str(resolved), stat.st_mtime_ns, stat.st_size
    with _LOCK:
        digest = _HASHES.get(key)
    if digest is None:
        with resolved.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with _LOCK:
            if len(_HASHES) > 2048:
                _HASHES.clear()
            _HASHES[key] = digest
    if digest in info["hashes"]:
        raise SkillError("这个副本属于KP受保护资料，不能通过改名或别的聊天读取")
    if resolved.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(resolved) as archive:
                entries = archive.infolist()
                if len(entries) > 500 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
                    raise SkillError("压缩包过大，不能确认是否包含KP资料")
                for entry in entries:
                    if not entry.is_dir():
                        check_payload(archive.read(entry), policy)
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise SkillError("无法确认压缩包资料权限，请解开后逐文件读取") from error


def public_event(event, policy):
    return (
        event.meta.get("visibility") not in {"kp", "private", "character"}
        and not event.meta.get("trpg_secret")
        and event.id not in load(policy)["events"]
    )


def public_memory(memory, policy):
    info = load(policy)
    return not (
        set(getattr(memory, "event_ids", [])) & (set(info["events"]) | set(info.get("fiction", [])))
    )


def is_fiction(event, policy):
    return bool(event.meta.get("fiction") or event.id in load(policy).get("fiction", []))

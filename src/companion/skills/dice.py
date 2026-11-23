"""真实骰子与群内约定的 CoC 七版检定；程序出目，模型只负责解释。"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from companion.config import DiceSkillConfig
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

MAX_LENGTH, MAX_DICE, MAX_SIDES = 160, 100, 1_000_000
MAX_VALUE, MAX_NODES, MAX_DEPTH = 1_000_000_000, 64, 12
RULESET = "coc7-group-20261111"
HELP = """真实投骰（不调用模型、不自动重投）
/掷骰 2d6+6 或 .r 2d6+6；省略表达式默认 d100
支持 NdM、整数、+、-、括号；d100 = 1d100
/检定 60｜常规｜奖1｜幸运（也可写 .ra 60 困难 奖1 侦查）
难度：常规/困难/极难，默认常规；奖1/奖2、罚1/罚2，可先抵消
按群内七版约定显示真实出目、成功等级和是否达到难度
/骰记录 查询当前聊天最近5条；可填1～10或记录编号
默认关闭房规；本功能不修改角色卡、SAN或剧情。"""


@dataclass(frozen=True)
class Node:
    kind: str
    value: int = 0
    sides: int = 0
    left: Node | None = None
    right: Node | None = None


class Expression:
    """受限语法，完整解析和预算检查通过以后才会取随机数；没有 eval。"""

    def __init__(self, text: str) -> None:
        if len(text) > MAX_LENGTH:
            raise SkillError(f"表达式最多 {MAX_LENGTH} 字符")
        self.text = re.sub(r"\s+", "", text).lower()
        if not self.text or re.search(r"[^0-9d()+-]", self.text):
            raise SkillError("只支持 NdM、整数、+、-、括号，例如 2d6+6、(d6+2)-1")
        self.pos, self.count, self.nodes = 0, 0, 0
        self.root = self._expr(0)
        if self.pos != len(self.text):
            raise SkillError("表达式没有完整匹配，请检查运算符和括号")
        low, high = self._bounds(self.root)
        if max(abs(low), abs(high)) > MAX_VALUE:
            raise SkillError("表达式可能产生的结果过大")

    def _peek(self) -> str:
        return self.text[self.pos:self.pos + 1]

    def _node(self, kind, value=0, sides=0, left=None, right=None):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise SkillError("表达式项数过多")
        return Node(kind, value, sides, left, right)

    def _integer(self) -> int | None:
        start = self.pos
        while self._peek().isdigit():
            self.pos += 1
        if start == self.pos:
            return None
        if self.pos - start > 10:
            raise SkillError("数字过大")
        value = int(self.text[start:self.pos])
        if value > MAX_VALUE:
            raise SkillError("数字过大")
        return value

    def _expr(self, depth: int) -> Node:
        node = self._atom(depth)
        while self._peek() and self._peek() in "+-":
            op = self._peek()
            self.pos += 1
            node = self._node(op, left=node, right=self._atom(depth))
        return node

    def _atom(self, depth: int) -> Node:
        if depth > MAX_DEPTH:
            raise SkillError("括号或正负号嵌套过深")
        token = self._peek()
        if token and token in "+-":
            self.pos += 1
            return self._node("sign", value=1 if token == "+" else -1,
                              left=self._atom(depth + 1))
        if token == "(":
            self.pos += 1
            node = self._expr(depth + 1)
            if self._peek() != ")":
                raise SkillError("括号没有配对")
            self.pos += 1
            return node
        number = self._integer()
        if self._peek() == "d":
            self.pos += 1
            count = 1 if number is None else number
            sides = self._integer()
            if not 1 <= count <= MAX_DICE or sides is None or not 1 <= sides <= MAX_SIDES:
                raise SkillError(f"每项骰数1～{MAX_DICE}，面数1～{MAX_SIDES}")
            self.count += count
            if self.count > MAX_DICE:
                raise SkillError(f"一次表达式合计最多 {MAX_DICE} 颗骰子")
            return self._node("dice", count, sides)
        if number is None:
            raise SkillError("这里需要整数、骰子或括号")
        return self._node("constant", number)

    def _bounds(self, node: Node) -> tuple[int, int]:
        if node.kind == "dice":
            result = node.value, node.value * node.sides
        elif node.kind == "constant":
            result = node.value, node.value
        elif node.kind == "sign":
            low, high = self._bounds(node.left)
            result = (low, high) if node.value == 1 else (-high, -low)
        else:
            a, b = self._bounds(node.left)
            c, d = self._bounds(node.right)
            result = (a + c, b + d) if node.kind == "+" else (a - d, b - c)
        if max(abs(result[0]), abs(result[1])) > MAX_VALUE:
            raise SkillError("中间结果可能过大")
        return result

    def roll(self, randbelow: Callable[[int], int]) -> dict:
        terms = []

        def visit(node):
            if node.kind == "constant":
                return node.value
            if node.kind == "dice":
                values = [randbelow(node.sides) + 1 for _ in range(node.value)]
                terms.append({"expression": f"{node.value}d{node.sides}", "values": values,
                              "sum": sum(values)})
                return sum(values)
            left = visit(node.left)
            if node.kind == "sign":
                return node.value * left
            right = visit(node.right)
            return left + right if node.kind == "+" else left - right

        return {"expression": self.text, "total": visit(self.root), "terms": terms}


def check_spec(arg: str) -> dict:
    if len(arg) > 240:
        raise SkillError("检定参数过长")
    tokens = arg.replace("｜", " ").replace("|", " ").split()
    if not tokens or not re.fullmatch(r"[0-9]{1,3}", tokens[0]):
        raise SkillError("请给完整技能值：/检定 60｜困难｜奖1｜侦查")
    skill = int(tokens.pop(0))
    if not 1 <= skill <= 999:
        raise SkillError("技能值必须是1～999的整数")
    difficulty, bonus, penalty, label = "常规", 0, 0, []
    seen = set()
    aliases = {"常规": "常规", "普通": "常规", "困难": "困难", "极难": "极难"}
    for token in tokens:
        if token in aliases:
            if "difficulty" in seen:
                raise SkillError("难度只能指定一次")
            difficulty = aliases[token]
            seen.add("difficulty")
        elif match := re.fullmatch(r"(奖励|惩罚|奖|罚|b|p)([0-9]{1,2})", token, re.I):
            kind = "bonus" if match[1].lower() in {"奖励", "奖", "b"} else "penalty"
            if kind in seen or int(match[2]) > 20:
                raise SkillError("同类奖惩只写一次，每类最多20颗，抵消后最多2颗")
            seen.add(kind)
            if kind == "bonus":
                bonus = int(match[2])
            else:
                penalty = int(match[2])
        elif token.startswith(("奖", "罚", "惩罚")) or re.fullmatch(r"[bBpP][0-9+-].*", token):
            raise SkillError("奖惩格式为奖1、罚2；不接受负数或小数")
        else:
            label.append(token)
    if abs(bonus - penalty) > 2:
        raise SkillError("奖励和惩罚先抵消，剩余最多2颗；超出时请由KP明确裁定")
    if len(" ".join(label)) > 80:
        raise SkillError("检定说明最多80字")
    return {"skill": skill, "difficulty": difficulty, "bonus": bonus, "penalty": penalty,
            "label": " ".join(label)}


def grade(roll: int, skill: int, difficulty: str) -> dict:
    target = skill // {"常规": 1, "困难": 2, "极难": 5}[difficulty]
    if roll == 1:
        level, rank = "大成功", 4
    elif roll == 100 or (skill < 50 and roll >= 96):
        level, rank = "大失败", -1
    elif roll <= skill // 5:
        level, rank = "极难成功", 3
    elif roll <= skill // 2:
        level, rank = "困难成功", 2
    elif roll <= skill:
        level, rank = "常规成功", 1
    else:
        level, rank = "失败", 0
    return {"target": target, "level": level,
            "passed": rank >= {"常规": 1, "困难": 2, "极难": 3}[difficulty]}


def percentile(spec: dict, randbelow: Callable[[int], int]) -> dict:
    net = spec["bonus"] - spec["penalty"]
    units = randbelow(10)
    tens = [randbelow(10) * 10 for _ in range(1 + abs(net))]
    candidates = [t + units or 100 for t in tens]
    roll = min(candidates) if net > 0 else max(candidates)
    return {**spec, "units": units, "tens": tens, "candidates": candidates, "roll": roll,
            "net_bonus": max(0, net), "net_penalty": max(0, -net),
            **grade(roll, spec["skill"], spec["difficulty"])}


def command(text: str) -> tuple[str, str] | None:
    """只认明确骰子指令，不抢普通文本/其他机器人的 .r 开头单词。"""
    text = text.strip()
    if text[:1] in {"/", "／"}:
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return None
        head, tail = parts[0], parts[1] if len(parts) == 2 else ""
        marker = {"掷骰": "掷骰", "摇骰": "掷骰", "骰子": "掷骰", "r": "掷骰",
                  "roll": "掷骰", "检定": "检定", "ra": "检定", "骰记录": "骰记录",
                  "骰子帮助": "骰子帮助"}.get(head.lower())
        return (marker, tail.strip()) if marker else None
    if match := re.fullmatch(r"[.．](ra|r)(?=$|\s|[0-9dD(+-])(.*)", text, re.I):
        return ("检定" if match[1].lower() == "ra" else "掷骰", match[2].strip())
    return None


def format_record(row: dict) -> str:
    result = row["result"]
    head = f"骰子记录 {row['id']}（{row['ts']}）"
    if row["kind"] == "roll":
        lines = [head, f"{result['expression']} = {result['total']}"]
        lines += [f"{t['expression']}：{t['values']}，小计 {t['sum']}" for t in result["terms"]]
        return "\n".join(lines)
    choice = f"奖励{result['net_bonus']}" if result["net_bonus"] else (
        f"惩罚{result['net_penalty']}" if result["net_penalty"] else "无奖惩")
    return (f"{head}\n{result['label'] or '百分比检定'}：技能{result['skill']}，"
            f"{result['difficulty']}目标{result['target']}\n"
            f"奖{result['bonus']} / 罚{result['penalty']} → {choice}\n"
            f"共用个位 {result['units']}；十位 {', '.join(f'{t:02}' for t in result['tens'])}\n"
            f"候选 {result['candidates']} → D100={result['roll']}\n"
            f"{result['level']}；{'达到' if result['passed'] else '未达到'}所需难度。")


class Dice(Skill):
    name, label, bare = "dice", "真实骰子", True
    markers = {
        "掷骰": "【掷骰：2d6+6】真实随机投骰，支持NdM、整数、加减和括号；省略默认d100；显示每颗和总和。",
        "检定": "【检定：60｜困难｜奖1｜侦查】CoC七版，完整技能值、难度(常规/困难/极难)、"
        "可选奖1/奖2或罚1/罚2、说明；奖惩抵消，共用个位；完整技能值决定大失败。",
        "骰记录": "【骰记录：5】查看当前聊天最近1～10条真实结果，也可填记录编号；不能查别的聊天。",
    }

    def __init__(self, cfg: DiceSkillConfig, path: Path,
                 randbelow: Callable[[int], int] = secrets.randbelow) -> None:
        self.cfg, self.path, self.randbelow = cfg, path, randbelow

    def enabled(self) -> bool:
        return self.cfg.enabled

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS rolls (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, body TEXT NOT NULL, channel TEXT NOT NULL,
            person_id TEXT NOT NULL, kind TEXT NOT NULL, request_key TEXT, request TEXT NOT NULL,
            result TEXT NOT NULL, UNIQUE(body, channel, person_id, request_key))""")
        return connection

    @staticmethod
    def _record(row) -> dict:
        return {**dict(row), "result": json.loads(row["result"])}

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        if not self.enabled():
            raise SkillError("骰子功能已关闭")
        if ctx.stop_event and ctx.stop_event.is_set():
            raise SkillError("任务已停止，不再掷骰")
        if marker == "骰记录":
            return SkillResult(self.history(arg, ctx), max_chars=18000)
        if marker not in {"掷骰", "检定"}:
            raise SkillError("未知骰子指令")
        expression = Expression(arg.strip() or "d100") if marker == "掷骰" else None
        spec = {"expression": expression.text} if expression else check_spec(arg)
        request = json.dumps({"marker": marker, "spec": spec, "ruleset": RULESET}, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if ctx.request_id:
                old = connection.execute(
                    "SELECT * FROM rolls WHERE body=? AND channel=? AND person_id=? AND request_key=?",
                    (ctx.body, ctx.channel, ctx.person_id, ctx.request_id)).fetchone()
                if old is not None:
                    if old["request"] != request:
                        raise SkillError("相同请求编号的骰子参数发生变化，拒绝重投；请发送新指令")
                    return SkillResult(format_record(self._record(old)), max_chars=18000,
                                       note="dice-replay")
            result = expression.roll(self.randbelow) if expression else percentile(spec, self.randbelow)
            result["ruleset"], result["random_source"] = RULESET, "os-secrets-randbelow"
            row = {"id": uuid4().hex[:16], "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                   "body": ctx.body, "channel": ctx.channel, "person_id": ctx.person_id,
                   "kind": "roll" if expression else "check", "request_key": ctx.request_id,
                   "request": request, "result": result}
            connection.execute("INSERT INTO rolls VALUES (?,?,?,?,?,?,?,?,?)",
                               (*[row[k] for k in ("id", "ts", "body", "channel", "person_id", "kind",
                                                   "request_key", "request")],
                                json.dumps(result, ensure_ascii=False)))
            connection.commit()  # 先落盘，成功后才向模型或QQ公布。
            return SkillResult(format_record(row), max_chars=18000)
        finally:
            connection.close()

    def history(self, arg: str, ctx: SkillContext) -> str:
        arg = arg.strip()
        connection = self._connect()
        try:
            if re.fullmatch(r"[0-9a-f]{16}", arg):
                rows = connection.execute("SELECT * FROM rolls WHERE body=? AND channel=? AND id=?",
                                          (ctx.body, ctx.channel, arg)).fetchall()
            else:
                if arg and (not re.fullmatch(r"[0-9]{1,2}", arg) or not 1 <= int(arg) <= 10):
                    raise SkillError("请填1～10或当前聊天的记录编号")
                rows = connection.execute(
                    "SELECT * FROM rolls WHERE body=? AND channel=? ORDER BY rowid DESC LIMIT ?",
                    (ctx.body, ctx.channel, int(arg or 5))).fetchall()
            return "\n\n".join(format_record(self._record(r)) for r in rows) or "当前聊天没有这些骰子记录。"
        finally:
            connection.close()

    def context(self, ctx: SkillContext) -> str:
        return ("掷骰必须调用真实骰子工具，不能编数字；如实使用结果和编号，不因失败自动重投。"
                "按已有玩家授权和KP宣告的目标掷骰；没有新授权不得刷点。奖惩最多2颗，房规关闭。"
                "检定结果只判断成功等级，不修改角色卡/SAN，不自行编造伤害、对抗或疯狂后果。"
                "本工具不会自动把群内某条待定检定结算，请确认当前行动再掷骰。")

    def status(self) -> str:
        return "操作系统随机；当前聊天审计记录；CoC七版基础/百分比/奖惩"

"""模拟 `codex app-server` 的 JSON-RPC 行为，供测试使用。

环境变量：
  FAKE_USED_PERCENT  额度窗口已用百分比（默认 10）
  FAKE_FAIL          usage_limit：本轮以额度错误失败
  FAKE_REPLY         回复文本（默认“你好呀”）
"""

import json
import os
import sys
import time

used = float(os.environ.get("FAKE_USED_PERCENT", "10"))
fail = os.environ.get("FAKE_FAIL", "")
reply = os.environ.get("FAKE_REPLY", "你好呀")
reset_at = int(time.time()) + 3600


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


threads = 0
while True:
    msg = read()
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        pass
    elif method == "account/rateLimits/read":
        window = {"usedPercent": used, "windowDurationMins": 300, "resetsAt": reset_at}
        send({"id": rid, "result": {"rateLimits": {"primary": window, "secondary": None}}})
    elif method == "thread/start":
        assert params.get("approvalPolicy") == "never" and params.get("sandbox") == "read-only"
        assert params.get("ephemeral") is True and "对话代理" in params.get("baseInstructions", "")
        threads += 1
        send({"id": rid, "result": {"thread": {"id": f"t{threads}"}}})
    elif method == "turn/start":
        tid = params["threadId"]
        text = params["input"][0]["text"]
        send({"id": rid, "result": {"turn": {"id": "u1", "status": "inProgress"}}})
        # 中途发来一个审批请求，客户端应当拒绝并继续
        send({"id": 900, "method": "item/commandExecution/requestApproval", "params": {}})
        answer = read()
        assert answer and answer.get("id") == 900 and "error" in answer
        if fail == "usage_limit":
            send({"method": "turn/completed", "params": {"threadId": tid, "turn": {
                "id": "u1", "status": "failed",
                "error": {"message": "You've hit your usage limit."}}}})  # fmt: skip
            continue
        out = '{"ok": true}' if "outputSchema" in params else reply
        images = [i for i in params["input"] if i.get("type") == "localImage"]
        if images:
            assert all(os.path.isfile(i["path"]) for i in images)
            out = f"我看到了 {len(images)} 张图"
        if "<设定>" not in text:
            out = "没有收到设定"
        half = len(out) // 2
        for piece in (out[:half], out[half:]):
            send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "delta": piece}})
        send({"method": "item/completed", "params": {
            "threadId": tid, "item": {"type": "agentMessage", "text": out}}})  # fmt: skip
        send({"method": "turn/completed", "params": {
            "threadId": tid, "turn": {"id": "u1", "status": "completed"}}})  # fmt: skip
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})

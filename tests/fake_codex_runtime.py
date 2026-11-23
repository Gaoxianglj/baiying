"""真实 stdio 边界上的动态工具与 steer/interrupt 测试服务，不连接任何外部服务。"""
import json
import os
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
mode = os.environ.get("RUNTIME_MODE", "tool")
specs = []


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


while (m := read()) is not None:
    method, rid, p = m.get("method"), m.get("id"), m.get("params", {})
    if method == "initialize":
        assert p["capabilities"]["experimentalApi"]
        send({"id": rid, "result": {}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        specs = p.get("dynamicTools", [])
        if specs:
            assert "文件操作仅能调用" in p["baseInstructions"]
        send({"id": rid, "result": {"thread": {"id": "runtime-thread"}}})
    elif method == "turn/start":
        tid, uid = p["threadId"], "runtime-turn"
        send({"id": rid, "result": {"turn": {"id": uid, "status": "inProgress"}}})
        if mode == "agent-result":
            text = os.environ["RUNTIME_RESULT"]
        elif mode.startswith("tool"):
            tool = specs[0]["name"]
            params = {"threadId": tid if mode != "tool-foreign" else "other-thread",
                      "turnId": uid, "callId": "one-call", "tool": tool,
                      "arguments": {"arg": "hello", "content": ""}, "namespace": None}
            send({"method": "item/started", "params": {"threadId": tid, "turnId": uid,
                  "item": {"id": "item-1", "type": "dynamicToolCall"}}})
            for request_id in (90, 91):
                send({"id": request_id, "method": "item/tool/call", "params": params})
                response = read()
                assert response["id"] == request_id
            result = response["result"]
            text = result["contentItems"][0]["text"]
            send({"method": "item/completed", "params": {"threadId": tid, "turnId": uid,
                  "item": {"id": "item-1", "type": "dynamicToolCall"}}})
        else:
            send({"method": "thread/status/changed", "params": {"threadId": tid,
                  "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}}})
            control = read()
            assert control["method"] in {"turn/steer", "turn/interrupt"}
            send({"id": control["id"], "result": {"turnId": uid}})
            if control["method"] == "turn/interrupt":
                send({"method": "turn/completed", "params": {"threadId": tid,
                      "turn": {"id": uid, "status": "interrupted"}}})
                continue
            assert control["params"]["expectedTurnId"] == uid
            text = control["params"]["input"][0]["text"]
        send({"method": "item/completed", "params": {"threadId": tid, "turnId": uid,
              "item": {"type": "agentMessage", "text": text}}})
        send({"method": "turn/completed", "params": {"threadId": tid,
              "turn": {"id": uid, "status": "completed"}}})
    elif rid is not None:
        send({"id": rid, "result": {}})

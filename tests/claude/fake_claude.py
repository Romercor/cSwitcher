"""Stand-in for `claude -p` in stream-json mode: reports its arguments, answers each
user message with an assistant message and a result, writes a non-JSON line too, and
exits when its input closes."""

import json
import sys


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "args": sys.argv[1:]})
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "user":
        text = message["message"]["content"]
        print("not json", flush=True)
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"echo {text}"}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text})

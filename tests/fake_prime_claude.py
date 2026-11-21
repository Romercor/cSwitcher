"""Stand-in for `claude -p` in the priming tests: records its arguments, config dir, the
login overrides it saw and which accounts cSwitcher's state marks as being primed,
simulates a token refresh in its config, then sleeps FAKE_CLAUDE_SLEEP seconds and
exits with FAKE_CLAUDE_EXIT."""

import json
import os
import sys
import time
from pathlib import Path

state_file = Path(os.environ["FAKE_CLAUDE_STATE"])
marked = sorted(json.loads(state_file.read_text(encoding="utf-8")).get("priming", {})) if state_file.exists() else []
with open(os.environ["FAKE_CLAUDE_RECORD"], "a", encoding="utf-8") as record:
    record.write(json.dumps({"args": sys.argv[1:], "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "marked": marked,
                             "overrides": sorted(k for k in ("ANTHROPIC_API_KEY", "CLAUDE_SECURESTORAGE_CONFIG_DIR")
                                                 if k in os.environ)}) + "\n")
credentials = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
data = json.loads(credentials.read_text(encoding="utf-8"))
data["claudeAiOauth"]["refreshToken"] += "-refreshed"
credentials.write_text(json.dumps(data), encoding="utf-8")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
print("OK")
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))

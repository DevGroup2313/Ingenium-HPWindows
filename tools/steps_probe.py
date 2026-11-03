#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 24 October 2026, the owner: "The break it down into steps do not really work on Ingenium, the model never writes
# anything and an error message populates". This asks the app's own engine (llama.cpp's llama-server, started with the
# app's own arguments) what the page asks it when Break it down is pressed: the page's system text
# (tools/steps_probe_system.txt, taken from the page), the page's request, its sampling, with thinking switched off,
# streamed as the page reads it. For each model it shows what came back as the answer, what came back as thoughts,
# why it stopped, and the steps the page's reader takes from it.
#   python tools/steps_probe.py <engine folder> <model.gguf> [<model.gguf> ...]
#   STEPS_MAX: the answer's length in tokens (the page asked for 420 in 6.3)
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = open(os.path.join(HERE, "steps_probe_system.txt"), encoding="utf-8").read()
TASKS = ["write the grant report", "plan my sister's birthday party", "clean the kitchen before the guests come"]


def prompt(title):
    return ("Break this task into small, concrete steps for me.\n\nTask: " + title + "\n\nRules:\n- Between 3 and 8 steps.\n"
            "- Each step is one action I can picture myself doing, and starts with a verb.\n- The first step takes less than five minutes.\n"
            "- Plain words. No advice, no explanations, no headings.\n\nWrite only the numbered list.")


def parse_steps(txt):
    out = []
    for line in str(txt or "").splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*•]|step\s+\d+[:.)]?)\s+(.+?)\s*$", line, re.I)
        if not m:
            continue
        t = re.sub(r"\*\*|__", "", m.group(1))
        t = re.sub(r"^\[\s?\]\s*", "", t).strip()
        if len(t) > 1:
            out.append(t[:240])
    return out[:10]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ask(port, title, max_tokens):
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(title)}],
            "stream": True, "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
            "temperature": 0.45, "top_p": 0.8, "repeat_penalty": 1.1, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=json.dumps(body).encode("utf-8"),
                                 headers={"content-type": "application/json"})
    content, think, finish, t0, first = "", "", None, time.time(), None
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if not d or d == "[DONE]":
                continue
            ch = json.loads(d)
            c = (ch.get("choices") or [{}])[0] if ch.get("choices") else {}
            dl = c.get("delta") or {}
            if dl.get("reasoning_content"):
                think += dl["reasoning_content"]
            if dl.get("content"):
                if first is None:
                    first = time.time() - t0
                content += dl["content"]
            if c.get("finish_reason"):
                finish = c["finish_reason"]
    return content, think, finish, time.time() - t0, first


def main():
    eng = sys.argv[1]
    exe = os.path.join(eng, "llama-server.exe" if os.name == "nt" else "llama-server")
    mx = int(os.environ.get("STEPS_MAX", "420"))
    for model in sys.argv[2:]:
        port = free_port()
        args = [exe, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-c", "8192", "-np", "1", "--jinja",
                "--no-webui", "--cache-reuse", "256", "-ngl", "0"]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(600):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(1)
            print("## %s (answer at most %d tokens, thinking switched off)" % (os.path.basename(model), mx))
            for title in TASKS:
                content, think, finish, secs, first = ask(port, title, mx)
                stripped = re.sub(r"<think>[\s\S]*?</think>", "", content)
                stripped = re.sub(r"^[\s\S]*</think>", "", stripped).strip()
                steps = parse_steps(stripped)
                print("* Task: %s" % title)
                print("  * stopped because: %s; %.1f s; first word of the answer after %s" % (finish, secs, ("%.1f s" % first) if first is not None else "never"))
                print("  * thoughts: %d characters%s" % (len(think), (": " + json.dumps(think[:160])) if think else ""))
                print("  * answer (%d characters): %s" % (len(stripped), json.dumps(stripped[:400])))
                print("  * steps the page reads from it: %d %s" % (len(steps), json.dumps(steps[:8])))
            print()
        finally:
            proc.terminate()
            try:
                proc.wait(20)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Reference impl for: chat-fixes-s4b-build-request-turns

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'dashboard_chat.py'
t = p.read_text()

OLD = r'''    for msg in messages:
        content.append({"type": "text", "text": f"{msg.get('role', 'user')}: {msg.get('content', '')}"})

    for img in images:
        mime = img.get("mime", "image/png")
        encoded = img.get("encoded", "")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"}
        })

    return {"messages": [{"role": "system", "content": content}], "files": files}'''

NEW = r'''    turns = []
    last_user = None
    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        turns.append({"role": role, "content": msg.get("content", "")})
        if role == "user":
            last_user = i
    if images and last_user is not None:
        parts = [{"type": "text", "text": turns[last_user]["content"]}]
        for img in images:
            mime = img.get("mime", "image/png")
            encoded = img.get("encoded", "")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        turns[last_user]["content"] = parts

    return {"messages": [{"role": "system", "content": content}] + turns, "files": files}'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")

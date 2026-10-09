# Claim (a0be91760fb5, HIGH dashboard_chat.py): the deletion removes the logic that
# processed prior conversation turns into the system message's content.
# (The task REQUIRED moving turns out of the system message into real turns; the
# property the claim protects is "prior turns still reach the model".)
import dashboard_chat


def reproduce():
    msgs = [{"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"}]
    req = dashboard_chat.build_request("proj", msgs, [], [])
    flat = repr(req["messages"])
    for t in ("first question", "first answer", "second question"):
        assert t in flat, f"prior turn {t!r} lost from the request"

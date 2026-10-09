# Claim (a330fff2d326, HIGH dashboard_chat.py:284): build_request mutates the input
# `messages` list by replacing the content of the last user turn with a list.
import copy
import dashboard_chat


def reproduce():
    messages = [{"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "look at this"}]
    before = copy.deepcopy(messages)
    images = [{"mime": "image/png", "encoded": "aGVsbG8="}]
    req = dashboard_chat.build_request("proj", messages, [], images)
    assert isinstance(req["messages"][-1]["content"], list)   # the image did attach
    assert messages == before, "build_request mutated its input messages"

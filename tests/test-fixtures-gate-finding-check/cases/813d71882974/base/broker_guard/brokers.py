"""Load and normalize broker records from a parsed brokers.json payload."""


def load_brokers(data):
    """Dedupe broker records by id, keeping the FIRST occurrence of each id.

    Records without an "id" are all kept; input order is preserved and no
    record is mutated. Raises ValueError if the top level is not a dict with
    a 'brokers' key (including when it is a list or any other non-dict).
    """
    if not isinstance(data, dict) or "brokers" not in data:
        raise ValueError("expected an object with a 'brokers' list")
    seen = set()
    out = []
    for record in data["brokers"]:
        rid = record.get("id")
        if rid is None:
            out.append(record)
        elif rid not in seen:
            seen.add(rid)
            out.append(record)
    return out

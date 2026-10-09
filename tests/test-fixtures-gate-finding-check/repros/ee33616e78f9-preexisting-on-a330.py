# Real defect, PRE-EXISTING relative to a330fff2d326 (ee33616e78f9, HIGH, never fixed):
# stores_image picks the next index from the COUNT of files, so after a gap it
# overwrites an existing image. a330's diff does not touch stores_image, so this
# reproducer fails identically on a330's baseline and head -> 'pre-existing'.
import tempfile
import dashboard_chat as dc

SID = "a" * 32   # validate_session_id: ^[0-9a-f]{32}$
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def reproduce():
    base = tempfile.mkdtemp()
    a = dc.stores_image(base, SID, PNG + b"A")
    b = dc.stores_image(base, SID, PNG + b"B")
    import os
    os.remove(a)                      # a gap: one file left, named 1.png
    dc.stores_image(base, SID, PNG + b"C")
    with open(b, "rb") as fh:
        assert fh.read() == PNG + b"B", "storing a new image overwrote an existing one"

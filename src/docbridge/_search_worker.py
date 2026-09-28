"""One search, run in its own process so the server can bound its time.

Audit K1 (2026-09-26): Python's regular-expression engine backtracks, and a
pattern such as (a+)+$ can run far longer than any caller will wait. A returned-
match limit does not bound the time spent finding a match, and a thread cannot
be stopped - a process can. reading.search starts this script with a timeout
and kills it when the timeout passes. Literal searches run here too: a long
query over repetitive text is slow in the same engine.

Protocol. stdin: one JSON header line {"pattern", "flags", "overlapping",
"start", "limit"}, then the text as UTF-8 (surrogatepass, so any str survives).
stdout: {"total": n, "spans": [[start, end], ...]}, where n counts the
non-empty matches and spans holds matches start .. start+limit-1 of them.
Standard library only; the server runs it with -I -S.
"""
import json
import re
import sys


def main() -> None:
    data = sys.stdin.buffer.read()
    nl = data.index(b"\n")
    req = json.loads(data[:nl])
    text = data[nl + 1:].decode("utf-8", "surrogatepass")
    rx = re.compile(req["pattern"], req["flags"])
    lo, hi = req["start"], req["start"] + req["limit"]
    total, spans = 0, []

    def matches():
        if not req["overlapping"]:
            yield from rx.finditer(text)
            return
        pos = 0             # every occurrence: the next search starts one past the last START
        while True:
            m = rx.search(text, pos)
            if m is None:
                return
            yield m
            pos = m.start() + 1

    for m in matches():
        if m.end() == m.start():
            continue
        if lo <= total < hi:
            spans.append([m.start(), m.end()])
        total += 1
    sys.stdout.write(json.dumps({"total": total, "spans": spans}))


if __name__ == "__main__":
    main()

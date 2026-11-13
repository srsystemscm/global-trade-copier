"""Strip the version pins off a requirements file, keeping the names.

For the upgrade canary only: a job that deliberately installs the newest
of everything, to answer "what breaks when we upgrade" before anybody
upgrades. The pins exist precisely so that normal installs do not do this.

Derived from the pinned files rather than keeping a second list of package
names, because a second list is one more thing to forget: a dependency
added to requirements.txt is canaried automatically, and one that is not
there cannot be silently left out.

    python scripts/unpin_requirements.py requirements.txt requirements-dev.txt

Prints one bare requirement per line, extras kept:

    fastapi
    uvicorn[standard]
    pyzmq
"""
import re
import sys
from pathlib import Path

# Everything from the first version operator onwards. `uvicorn[standard]`
# survives because `[` is not an operator; `ib_async>=2.1,<3` becomes
# `ib_async`; an environment marker after `;` goes too, since a canary that
# honoured one would be installing a different set from the real thing.
VERSION = re.compile(r"[=<>!~;].*$")


def names(path: Path) -> list:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = VERSION.sub("", line).strip()
        if name:
            out.append(name)
    return out


def main(argv: list) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    seen = set()
    for arg in argv:
        path = Path(arg)
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 1
        for name in names(path):
            key = re.sub(r"[-_.]+", "-", name.split("[")[0]).lower()
            if key in seen:
                continue
            seen.add(key)
            print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

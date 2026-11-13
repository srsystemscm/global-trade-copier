"""The Global Trade Copier hub."""

# The copier had no version string of its own until the Operations hub asked
# for one: `snapshot.copier.version` is how the hub's Copier tab says which
# build a report came from, and "unknown" there makes a report that disagrees
# with another impossible to place. 5.x because the README describes Phase 5
# hardening as the state this grew out of; the reporter is the addition on
# top of it.
__version__ = "5.1.0"

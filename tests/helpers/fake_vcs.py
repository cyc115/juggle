"""FakeBackend — in-memory VCS Protocol implementation for policy unit tests.

Records every method call (name + args/kwargs) and returns scripted results
set by the test, so policy code (stack_base, land-poller, state-machine edges)
can be pinned WITHOUT a real VCS (SPEC §6 test strategy).
"""

from __future__ import annotations

from vcs_types import Capabilities, LandStatus, SubmitResult, UpdateResult, WorkspaceResult


class FakeBackend:
    name = "fake"
    capabilities = Capabilities(async_land=True, auto_restack=False)

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.scripted: dict[str, object] = {}

    def _record(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        return self.scripted.get(method)

    def repo_root(self, path):
        return self._record("repo_root", path) or path

    def primary_root(self, repo):
        return self._record("primary_root", repo) or repo

    def refresh(self, repo):
        self._record("refresh", repo)

    def trunk(self, repo):
        r = self._record("trunk", repo)
        return r if r is not None else "trunk-rev"

    def resolve(self, repo, ref=None):
        r = self._record("resolve", repo, ref)
        return r if r is not None else (ref or "head-rev")

    def is_ancestor(self, repo, rev, of):
        r = self._record("is_ancestor", repo, rev, of)
        return bool(r) if r is not None else False

    def create_workspace(self, repo, name, root, *, base=None):
        r = self._record("create_workspace", repo, name, root, base=base)
        return r if r is not None else WorkspaceResult(ok=True, path=root, branch=name)

    def remove_workspace(self, repo, ws):
        r = self._record("remove_workspace", repo, ws)
        return bool(r) if r is not None else True

    def current_rev(self, ws):
        r = self._record("current_rev", ws)
        return r if r is not None else "ws-rev"

    def dirty_files(self, ws):
        r = self._record("dirty_files", ws)
        return r if r is not None else []

    def has_changes(self, ws, *, since):
        r = self._record("has_changes", ws, since=since)
        return bool(r) if r is not None else False

    def update_to(self, ws, base):
        r = self._record("update_to", ws, base)
        return r if r is not None else UpdateResult(ok=True)

    def describe_changes(self, ws, *, since):
        r = self._record("describe_changes", ws, since=since)
        return r if r is not None else ""

    def submit(self, ws, *, base, mode):
        r = self._record("submit", ws, base=base, mode=mode)
        return r if r is not None else SubmitResult(status="landed", landed_rev="landed-rev")

    def land_status(self, repo, ticket):
        r = self._record("land_status", repo, ticket)
        return r if r is not None else LandStatus(state="unlanded")

    def head(self, path):
        return self.resolve(path, None)

    def is_dirty(self, path):
        return bool(self.dirty_files(path))

    def make_safety_branch(self, path, sha, name):
        r = self._record("make_safety_branch", path, sha, name)
        return bool(r) if r is not None else True

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from scripts.ci.finalize_js_autofix import GitHubPort, Provenance, PullRequest, finalize


@dataclass
class FakePort:
    pr: PullRequest | None
    main: str = "base"
    parent: str = "base"
    current_pr_head: str = "head"
    branch_head: str = "head"
    trusted: bool = True
    required_gate: str = "success"
    provenance_base: str = "base"
    producer_conclusion: str = "success"
    actions: list[str] = field(default_factory=list)

    def find_pr(self, head_sha: str) -> PullRequest | None:
        return self.pr if self.pr and self.pr.head_sha == head_sha else None

    def main_sha(self) -> str:
        return self.main

    def main_contains(self, head_sha: str) -> bool:
        return self.main in {head_sha, "descendant"}

    def load_provenance(self, head_sha: str) -> Provenance | None:
        return Provenance(123, 1, head_sha, self.provenance_base, self.producer_conclusion) if self.trusted else None

    def required_gate_status(self, ci_run_id: int, ci_attempt: int, head_sha: str) -> str:
        return self.required_gate

    def parent_sha(self, head_sha: str) -> str:
        return self.parent

    def pr_head_is(self, number: int, head_sha: str) -> bool:
        return self.current_pr_head == head_sha

    def close_exact(self, number: int, branch: str, head_sha: str) -> bool:
        if self.branch_head != head_sha:
            return False
        self.actions.extend([f"delete:{branch}:{head_sha}", f"close:{number}"])
        self.branch_head = ""
        return True

    def promote(self, head_sha: str) -> None:
        self.actions.append(f"promote:{head_sha}")
        self.main = head_sha

    def delete_branch_if_head(self, branch: str, head_sha: str) -> None:
        if self.branch_head == head_sha:
            self.actions.append(f"delete:{branch}:{head_sha}")
            self.branch_head = ""

    def dispatch_main_workflows(self, head_sha: str) -> None:
        assert head_sha == "head"
        self.actions.extend(["dispatch-ci", "dispatch-docker", "dispatch-nix"])


_OPEN = PullRequest(number=4, state="OPEN", head_sha="head")
_DISPATCH = ["dispatch-ci", "dispatch-docker", "dispatch-nix"]


def test_finalizer_promotes_only_an_exact_trusted_green_lineage():
    cases = [
        ({}, "promoted", ["promote:head", "delete:bot/js-autofix:head", *_DISPATCH]),
        ({"main": "new-main"}, "stale-base", ["delete:bot/js-autofix:head", "close:4"]),
        ({"required_gate": "failure"}, "ci-failed", ["delete:bot/js-autofix:head", "close:4"]),
        ({"required_gate": "superseded"}, "validation-superseded", []),
        ({"current_pr_head": "new-head", "branch_head": "new-head"}, "superseded", []),
        ({"branch_head": "new-head"}, "promoted", ["promote:head", *_DISPATCH]),
        ({"pr": PullRequest(4, "MERGED", "head"), "main": "head"}, "post-merge-retry", _DISPATCH),
        ({"trusted": False}, "untrusted", []),
        ({"provenance_base": "other-base"}, "untrusted", []),
        ({"producer_conclusion": "timed_out"}, "producer-failed", ["delete:bot/js-autofix:head", "close:4"]),
        ({"pr": PullRequest(4, "CLOSED", "head"), "main": "head"}, "post-merge-retry", _DISPATCH),
        ({"pr": PullRequest(4, "CLOSED", "head"), "main": "descendant"}, "post-merge-retry", _DISPATCH),
    ]
    for overrides, expected_result, expected_actions in cases:
        overrides = dict(overrides)
        port = FakePort(pr=overrides.pop("pr", _OPEN), **overrides)

        result = finalize(port, head_sha="head", ci_run_id=456, ci_attempt=1)

        assert result == expected_result
        assert port.actions == expected_actions


def test_github_port_keeps_external_handoffs_recoverable(monkeypatch):
    port = GitHubPort("NousResearch/hermes-agent")
    dispatched: list[list[str]] = []

    def dispatch_run(args, *, check=True):
        if args[:3] == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(args, 0, "website/src/app.ts\n", "")
        dispatched.append(args[3:])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(port, "_run", dispatch_run)
    port.dispatch_main_workflows("head")
    assert dispatched == [
        ["ci.yaml", "--repo", "NousResearch/hermes-agent", "--ref", "main"],
        [
            "docker.yml",
            "--repo",
            "NousResearch/hermes-agent",
            "--ref",
            "main",
            "-f",
            "publish=true",
        ],
        ["nix.yml", "--repo", "NousResearch/hermes-agent", "--ref", "main"],
        ["deploy-site.yml", "--repo", "NousResearch/hermes-agent", "--ref", "main"],
    ]

    dispatched.clear()
    port.repo = "owner/fork"
    port.dispatch_main_workflows("head")
    assert dispatched == [
        ["ci.yaml", "--repo", "owner/fork", "--ref", "main"],
        ["docker.yml", "--repo", "owner/fork", "--ref", "main"],
        ["nix.yml", "--repo", "owner/fork", "--ref", "main"],
    ]

    downloads = 0

    def provenance_gh(*args):
        if args[-1].endswith("/artifacts"):
            return json.dumps({"artifacts": [{"name": "js-autofix-provenance-123-1-1"}]})
        return json.dumps(
            {
                "name": "auto-fix lint issues & formatting",
                "event": "push",
                "headBranch": "main",
                "headSha": "base",
                "conclusion": "success",
            }
        )

    def provenance_run(args, *, check=True):
        nonlocal downloads
        if args[:3] == ["git", "show", "-s"]:
            return subprocess.CompletedProcess(
                args,
                0,
                "Autofix-Producer-Run: 123\nAutofix-Producer-Attempt: 1\n",
                "",
            )
        downloads += 1
        return subprocess.CompletedProcess(args, 1, "", "temporary failure")

    monkeypatch.setattr(port, "_gh", provenance_gh)
    monkeypatch.setattr(port, "_run", provenance_run)
    monkeypatch.setattr("scripts.ci.finalize_js_autofix.time.sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="could not download provenance artifact"):
        port.load_provenance("head")
    assert downloads == 5

    run = {
        "name": "CI",
        "event": "workflow_dispatch",
        "headBranch": "bot/js-autofix",
        "headSha": "head",
        "jobs": [{"name": "Required lane", "conclusion": "cancelled"}],
    }
    replacement = False

    def gate_gh(*args):
        if args[0] == "api" and args[-1].endswith("/456"):
            return json.dumps({"run_attempt": 1})
        if args[:2] == ("run", "view"):
            return json.dumps(run)
        return json.dumps(
            [{"databaseId": 123, "headSha": "head", "status": "in_progress", "conclusion": ""}]
            if replacement
            else []
        )

    monkeypatch.setattr(port, "_gh", gate_gh)
    assert port.required_gate_status(456, 1, "head") == "failure"
    replacement = True
    assert port.required_gate_status(456, 1, "head") == "superseded"

    deletes = 0

    def delete_run(args, *, check=True):
        nonlocal deletes
        if args[:2] == ["git", "push"]:
            deletes += 1
            return subprocess.CompletedProcess(args, 0 if deletes == 3 else 1, "", "temporary failure")
        return subprocess.CompletedProcess(args, 0, "head\trefs/heads/bot/js-autofix\n", "")

    monkeypatch.setattr(port, "_run", delete_run)
    assert port._delete_branch_cas("bot/js-autofix", "head") is True
    assert deletes == 3

    pushes = 0

    def promote_run(args, *, check=True):
        nonlocal pushes
        if args[:2] == ["git", "push"]:
            pushes += 1
            return subprocess.CompletedProcess(args, 1, "", "connection lost")
        return subprocess.CompletedProcess(args, 0, "head", "")

    monkeypatch.setattr(port, "_run", promote_run)
    monkeypatch.setattr(port, "_main_sha_if_available", lambda: "head")
    port.promote("head")
    assert pushes == 1

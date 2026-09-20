#!/usr/bin/env python3
"""Finalize a GITHUB_TOKEN-created JS autofix PR after dispatched CI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


BOT_BRANCH = "bot/js-autofix"


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    head_sha: str


@dataclass(frozen=True)
class Provenance:
    run_id: int
    run_attempt: int
    head_sha: str
    base_sha: str
    producer_conclusion: str


class FinalizerPort(Protocol):
    def find_pr(self, head_sha: str) -> PullRequest | None: ...
    def main_sha(self) -> str: ...
    def main_contains(self, head_sha: str) -> bool: ...
    def load_provenance(self, head_sha: str) -> Provenance | None: ...
    def required_gate_status(self, ci_run_id: int, ci_attempt: int, head_sha: str) -> str: ...
    def parent_sha(self, head_sha: str) -> str: ...
    def pr_head_is(self, number: int, head_sha: str) -> bool: ...
    def close_exact(self, number: int, branch: str, head_sha: str) -> bool: ...
    def promote(self, head_sha: str) -> None: ...
    def delete_branch_if_head(self, branch: str, head_sha: str) -> None: ...
    def dispatch_main_workflows(self, head_sha: str) -> None: ...


def finalize(port: FinalizerPort, *, head_sha: str, ci_run_id: int, ci_attempt: int) -> str:
    """Promote exactly one validated bot commit or clean up its exact branch."""
    provenance = port.load_provenance(head_sha)
    if provenance is None or provenance.head_sha != head_sha:
        return "untrusted"

    pr = port.find_pr(head_sha)
    if pr is None:
        if port.main_contains(head_sha):
            port.dispatch_main_workflows(head_sha)
            return "post-merge-retry"
        return "no-pr"

    if provenance.producer_conclusion != "success":
        if pr.state == "OPEN" and port.close_exact(pr.number, BOT_BRANCH, head_sha):
            return "producer-failed"
        return "superseded"

    if pr.state == "MERGED":
        port.dispatch_main_workflows(head_sha)
        return "post-merge-retry"
    if pr.state == "CLOSED":
        if port.main_contains(head_sha):
            port.dispatch_main_workflows(head_sha)
            return "post-merge-retry"
        return "closed"
    if not port.pr_head_is(pr.number, head_sha):
        return "superseded"

    gate_status = port.required_gate_status(ci_run_id, ci_attempt, head_sha)
    if gate_status == "superseded":
        return "validation-superseded"
    if gate_status != "success":
        if not port.close_exact(pr.number, BOT_BRANCH, head_sha):
            return "superseded"
        return "ci-failed"

    if port.main_contains(head_sha):
        port.dispatch_main_workflows(head_sha)
        port.close_exact(pr.number, BOT_BRANCH, head_sha)
        return "post-merge-retry"

    validated_base = port.parent_sha(head_sha)
    if validated_base != provenance.base_sha:
        return "untrusted"
    if port.main_sha() != validated_base:
        if not port.close_exact(pr.number, BOT_BRANCH, head_sha):
            return "superseded"
        return "stale-base"
    if not port.pr_head_is(pr.number, head_sha):
        return "superseded"

    # The push is deliberately non-force. It succeeds only while main is still
    # the validated parent, giving the base/head handoff an atomic CAS gate.
    port.promote(head_sha)
    port.dispatch_main_workflows(head_sha)
    port.delete_branch_if_head(BOT_BRANCH, head_sha)
    return "promoted"


class GitHubPort:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    @staticmethod
    def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        # Callers pass argv arrays to trusted gh/git binaries; shell parsing is never enabled.
        return subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            args,
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

    def _gh(self, *args: str) -> str:
        return self._run(["gh", *args]).stdout.strip()

    def find_pr(self, head_sha: str) -> PullRequest | None:
        raw = self._gh(
            "pr", "list", "--repo", self.repo, "--head", BOT_BRANCH,
            "--base", "main", "--state", "all", "--limit", "20",
            "--json", "number,state,headRefOid",
        )
        for item in json.loads(raw):
            if item["headRefOid"] == head_sha:
                return PullRequest(item["number"], item["state"], item["headRefOid"])
        return None

    def main_sha(self) -> str:
        return self._gh("api", f"repos/{self.repo}/branches/main", "--jq", ".commit.sha")

    def main_contains(self, head_sha: str) -> bool:
        fetch = self._run(["git", "fetch", "origin", "main"], check=False)
        if fetch.returncode != 0:
            return False
        result = self._run(
            ["git", "merge-base", "--is-ancestor", head_sha, "origin/main"],
            check=False,
        )
        return result.returncode == 0

    def load_provenance(self, head_sha: str) -> Provenance | None:
        message = self._run(["git", "show", "-s", "--format=%B", head_sha]).stdout
        run_match = re.search(r"^Autofix-Producer-Run: ([1-9][0-9]*)$", message, re.MULTILINE)
        attempt_match = re.search(r"^Autofix-Producer-Attempt: ([1-9][0-9]*)$", message, re.MULTILINE)
        if run_match is None or attempt_match is None:
            return None
        run_id = int(run_match.group(1))
        run_attempt = int(attempt_match.group(1))
        run = json.loads(
            self._gh(
                "run", "view", str(run_id), "--repo", self.repo, "--attempt", str(run_attempt),
                "--json", "name,event,headSha,headBranch,conclusion",
            )
        )
        if not (
            run.get("name") == "auto-fix lint issues & formatting"
            and run.get("event") in {"push", "workflow_dispatch"}
            and run.get("headBranch") == "main"
            and isinstance(run.get("conclusion"), str)
            and bool(run["conclusion"])
        ):
            return None

        artifact_names = [f"js-autofix-provenance-{run_id}-{run_attempt}-{slot}" for slot in (2, 1)]
        artifacts = json.loads(self._gh("api", f"repos/{self.repo}/actions/runs/{run_id}/artifacts"))
        available = {item.get("name") for item in artifacts.get("artifacts", [])}
        artifact_name = next((name for name in artifact_names if name in available), None)
        if artifact_name is None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            for attempt in range(1, 6):
                result = self._run(
                    [
                        "gh", "run", "download", str(run_id), "--repo", self.repo,
                        "--name", artifact_name, "--dir", tmp,
                    ],
                    check=False,
                )
                if result.returncode == 0:
                    break
                time.sleep(attempt * 5)
            else:
                raise RuntimeError(f"could not download provenance artifact from run {run_id}")
            path = next(iter(Path(tmp).glob("*.json")), None)
            if path is None:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        if not (
            data.get("run_id") == run_id
            and data.get("run_attempt") == run_attempt
            and data.get("head_sha") == head_sha
            and data.get("base_sha") == run.get("headSha")
        ):
            return None
        return Provenance(
            run_id=run_id,
            run_attempt=run_attempt,
            head_sha=head_sha,
            base_sha=str(data.get("base_sha") or ""),
            producer_conclusion=str(run["conclusion"]),
        )

    def required_gate_status(self, ci_run_id: int, ci_attempt: int, head_sha: str) -> str:
        latest = json.loads(self._gh("api", f"repos/{self.repo}/actions/runs/{ci_run_id}"))
        if latest.get("run_attempt") != ci_attempt:
            return "superseded"
        run = json.loads(
            self._gh(
                "run", "view", str(ci_run_id), "--repo", self.repo, "--attempt", str(ci_attempt),
                "--json", "name,event,headSha,headBranch,jobs",
            )
        )
        if not (
            run.get("name") == "CI"
            and run.get("event") == "workflow_dispatch"
            and run.get("headBranch") == BOT_BRANCH
            and run.get("headSha") == head_sha
        ):
            return "failure"
        gates = [job for job in run.get("jobs", []) if job.get("name") == "All required checks pass"]
        clean = len(gates) == 1 and gates[0].get("conclusion") == "success"
        if clean:
            return "success"
        raw = self._gh(
            "run", "list", "--repo", self.repo, "--workflow", "ci.yaml",
            "--branch", BOT_BRANCH, "--event", "workflow_dispatch", "--limit", "20",
            "--json", "databaseId,headSha,status,conclusion",
        )
        replacement = any(
            item.get("databaseId") != ci_run_id
            and item.get("headSha") == head_sha
            and (item.get("status") != "completed" or item.get("conclusion") == "success")
            for item in json.loads(raw)
        )
        return "superseded" if replacement else "failure"

    def parent_sha(self, head_sha: str) -> str:
        return self._run(["git", "rev-parse", f"{head_sha}^"]).stdout.strip()

    def pr_head_is(self, number: int, head_sha: str) -> bool:
        raw = self._gh("pr", "view", str(number), "--repo", self.repo, "--json", "headRefOid,state")
        item = json.loads(raw)
        return item["state"] == "OPEN" and item["headRefOid"] == head_sha

    def _delete_branch_cas(self, branch: str, head_sha: str) -> bool:
        ref = f"refs/heads/{branch}"
        for attempt in range(1, 4):
            result = self._run(
                ["git", "push", f"--force-with-lease={ref}:{head_sha}", "origin", "--delete", branch],
                check=False,
            )
            if result.returncode == 0:
                return True
            probe = self._run(["git", "ls-remote", "--heads", "origin", ref], check=False)
            if probe.returncode == 0:
                remote_sha = probe.stdout.split(maxsplit=1)[0] if probe.stdout.strip() else ""
                if not remote_sha:
                    return True
                if remote_sha != head_sha:
                    return False
            time.sleep(attempt * 5)
        raise RuntimeError(f"could not verify or delete exact bot branch {branch}")

    def close_exact(self, number: int, branch: str, head_sha: str) -> bool:
        # Delete first with a lease. If a newer producer owns the branch, leave
        # its PR untouched rather than closing it through a stale run.
        if not self._delete_branch_cas(branch, head_sha):
            return False
        pr = json.loads(
            self._gh("pr", "view", str(number), "--repo", self.repo, "--json", "state,headRefOid")
        )
        if pr["state"] == "OPEN" and pr["headRefOid"] == head_sha:
            self._gh("pr", "close", str(number), "--repo", self.repo)
        return True

    def promote(self, head_sha: str) -> None:
        push_args = ["git", "push", "origin", f"{head_sha}:refs/heads/main"]
        for attempt in range(1, 6):
            self._run(push_args, check=False)
            if self.main_contains(head_sha):
                return
            if attempt < 5:
                time.sleep(attempt * 5)
        raise RuntimeError("main did not advance to the validated autofix head")

    def delete_branch_if_head(self, branch: str, head_sha: str) -> None:
        self._delete_branch_cas(branch, head_sha)

    def dispatch_main_workflows(self, head_sha: str) -> None:
        changed = self._run(["git", "diff", "--name-only", f"{head_sha}^", head_sha]).stdout.splitlines()
        workflows = ["ci.yaml", "docker.yml", "nix.yml"]
        if self.repo.casefold() == "nousresearch/hermes-agent" and any(
            path.startswith(("website/", "skills/", "optional-skills/", "plugin-catalog/"))
            or path == ".github/workflows/deploy-site.yml"
            for path in changed
        ):
            workflows.append("deploy-site.yml")
        for workflow in workflows:
            for attempt in range(1, 6):
                args = ["gh", "workflow", "run", workflow, "--repo", self.repo, "--ref", "main"]
                if workflow == "docker.yml" and self.repo.casefold() == "nousresearch/hermes-agent":
                    args.extend(["-f", "publish=true"])
                result = self._run(
                    args,
                    check=False,
                )
                if result.returncode == 0:
                    break
                time.sleep(attempt * 5)
            else:
                raise RuntimeError(f"could not dispatch post-merge workflow {workflow}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--ci-run-id", required=True, type=int)
    parser.add_argument("--ci-attempt", required=True, type=int)
    args = parser.parse_args()
    result = finalize(
        GitHubPort(args.repo),
        head_sha=args.head_sha,
        ci_run_id=args.ci_run_id,
        ci_attempt=args.ci_attempt,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

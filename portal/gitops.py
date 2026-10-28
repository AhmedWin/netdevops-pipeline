"""Open a pull request on GitHub containing a proposed network change.

The portal never applies a change. It writes a branch and opens a pull
request, which is then verified by exactly the same pipeline that guards
every other change. The UI is a nicer way to author a proposal - it is
not a way around review.
"""

import base64
import os
from dataclasses import dataclass

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


@dataclass
class Settings:
    token: str
    repo: str          # "owner/name"
    base_branch: str = "main"
    author_name: str = "NetDevOps Portal"
    author_email: str = "portal@example.invalid"

    @classmethod
    def from_env(cls):
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "").strip()
        if not token or not repo:
            return None
        return cls(
            token=token,
            repo=repo,
            base_branch=os.environ.get("GITHUB_BASE_BRANCH", "main").strip() or "main",
            author_name=os.environ.get("GIT_AUTHOR_NAME", "NetDevOps Portal"),
            author_email=os.environ.get("GIT_AUTHOR_EMAIL", "portal@example.invalid"),
        )


class GitHub:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.http = client or httpx.Client(
            timeout=30,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _call(self, method, path, **kwargs):
        response = self.http.request(method, f"{API}{path}", **kwargs)
        if response.status_code >= 400:
            detail = response.json().get("message", response.text) if response.content else ""
            raise GitHubError(f"{method} {path} -> {response.status_code}: {detail}")
        return response.json() if response.content else {}

    # -- the four calls that make a pull request ----------------------

    def base_sha(self) -> str:
        ref = self._call("GET", f"/repos/{self.s.repo}/git/ref/heads/{self.s.base_branch}")
        return ref["object"]["sha"]

    def create_tree(self, base_sha: str, files: dict) -> str:
        """files: {repo-relative path: text content}"""
        blobs = []
        for path, content in sorted(files.items()):
            blob = self._call(
                "POST",
                f"/repos/{self.s.repo}/git/blobs",
                json={
                    "content": base64.b64encode(content.encode()).decode(),
                    "encoding": "base64",
                },
            )
            blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

        base_commit = self._call("GET", f"/repos/{self.s.repo}/git/commits/{base_sha}")
        tree = self._call(
            "POST",
            f"/repos/{self.s.repo}/git/trees",
            json={"base_tree": base_commit["tree"]["sha"], "tree": blobs},
        )
        return tree["sha"]

    def create_commit(self, message: str, tree_sha: str, parent_sha: str) -> str:
        commit = self._call(
            "POST",
            f"/repos/{self.s.repo}/git/commits",
            json={
                "message": message,
                "tree": tree_sha,
                "parents": [parent_sha],
                "author": {"name": self.s.author_name, "email": self.s.author_email},
            },
        )
        return commit["sha"]

    def create_branch(self, name: str, sha: str):
        self._call(
            "POST",
            f"/repos/{self.s.repo}/git/refs",
            json={"ref": f"refs/heads/{name}", "sha": sha},
        )

    def open_pull_request(self, branch: str, title: str, body: str) -> dict:
        return self._call(
            "POST",
            f"/repos/{self.s.repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": branch,
                "base": self.s.base_branch,
            },
        )

    # -- the whole flow -----------------------------------------------

    def propose(self, branch: str, title: str, body: str, files: dict) -> dict:
        base = self.base_sha()
        tree = self.create_tree(base, files)
        commit = self.create_commit(title, tree, base)
        self.create_branch(branch, commit)
        pr = self.open_pull_request(branch, title, body)
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "branch": branch,
            "commit": commit[:7],
        }

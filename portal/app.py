"""NetDevOps Portal - a web front end for proposing network changes.

The portal does not contain any rules of its own. It calls the same
validate.py and render.py that the pipeline calls, so a change that looks
valid here is valid there, by construction. If those two could disagree,
the whole premise of the project would be gone.

What it does:
    - show the current network, read from intent/network.yml
    - let someone author a change through a form
    - validate that change in memory, before anything is written
    - show exactly which generated files the change would alter
    - open a pull request, which the pipeline then verifies

What it deliberately does not do:
    - apply anything to a device
    - merge anything
    - bypass review
"""

import copy
import difflib
import hmac
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# Works whether this is started as `portal.app:app` or run directly.
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(HERE))

from render import INTENT, build_device_models, load_intent, render_all, render_topology  # noqa: E402
from validate import validate_intent  # noqa: E402

from edit import EditError, apply_operation  # noqa: E402
from gitops import GitHub, GitHubError, Settings  # noqa: E402

STATIC = HERE / "static"

# A shared token is enough to keep a read-write tool off an open network,
# and is honestly not more than that. See the README before exposing this
# anywhere that matters.
PORTAL_TOKEN = os.environ.get("PORTAL_TOKEN", "").strip()
SESSION_COOKIE = "portal_session"
_SESSIONS: set[str] = set()

app = FastAPI(title="NetDevOps Portal", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

def require_session(session: str | None):
    if not PORTAL_TOKEN:
        return  # no token configured - open mode, for local use only
    if not session or session not in _SESSIONS:
        raise HTTPException(status_code=401, detail="Not signed in")


class Login(BaseModel):
    token: str


@app.post("/api/login")
def login(body: Login, response: Response):
    if not PORTAL_TOKEN:
        return {"ok": True, "auth": "disabled"}
    if not hmac.compare_digest(body.token, PORTAL_TOKEN):
        raise HTTPException(status_code=401, detail="Wrong token")
    session = secrets.token_urlsafe(32)
    _SESSIONS.add(session)
    response.set_cookie(
        SESSION_COOKIE, session, httponly=True, samesite="strict", max_age=28800
    )
    return {"ok": True, "auth": "enabled"}


@app.get("/api/session")
def session_state(portal_session: str | None = Cookie(default=None)):
    return {
        "auth_required": bool(PORTAL_TOKEN),
        "signed_in": not PORTAL_TOKEN or portal_session in _SESSIONS,
        "github_configured": Settings.from_env() is not None,
    }


# ---------------------------------------------------------------------
# Reading the network
# ---------------------------------------------------------------------

def summarise(intent: dict) -> dict:
    """The shape the UI draws: devices with their links and neighbours."""
    models = build_device_models(intent)
    return {
        "devices": [
            {
                "name": name,
                "asn": intent["devices"][name]["asn"],
                "loopback": intent["devices"][name]["loopback"],
                "interfaces": model["interfaces"],
                "neighbors": model["neighbors"],
            }
            for name, model in sorted(models.items())
        ],
        "links": [
            {
                "a": link["a"],
                "b": link["b"],
                "subnet": link["subnet"],
            }
            for link in intent.get("links", [])
        ],
    }


@app.get("/api/network")
def get_network(portal_session: str | None = Cookie(default=None)):
    require_session(portal_session)
    intent = load_intent(INTENT)
    report = validate_intent(intent, render_topology(intent))
    return {
        "intent": intent,
        # The file exactly as committed, comments and all. The raw editor
        # shows this rather than a regenerated version, so what someone
        # edits is what is actually in the repository.
        "intent_text": INTENT.read_text(),
        "summary": summarise(intent),
        "validation": report.to_dict(),
    }


# ---------------------------------------------------------------------
# Proposing a change
# ---------------------------------------------------------------------

class Proposal(BaseModel):
    """A change expressed as an operation, not as a finished document.

    The portal applies the operation to the committed intent file as
    text, so comments and formatting survive. Sending a whole rewritten
    document instead would silently strip every comment in the file.
    """

    operation: dict = Field(default_factory=lambda: {"kind": "none"})
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)


def build(operation: dict) -> tuple[str, dict]:
    """Apply an operation and parse the result. Raises on bad input."""
    original = INTENT.read_text()
    try:
        proposed_text = apply_operation(original, operation)
    except EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        proposed = yaml.safe_load(proposed_text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"That is not valid YAML: {exc}") from exc

    if not isinstance(proposed, dict):
        raise HTTPException(status_code=400, detail="The intent file must be a mapping")

    return proposed_text, proposed


def check(proposed: dict) -> dict:
    started = time.perf_counter()
    try:
        report = validate_intent(proposed, render_topology(proposed))
        result = report.to_dict()
    except Exception as exc:
        # A malformed intent can fail inside a check rather than be
        # reported by it. That is still a validation result, not a 500.
        result = {
            "ok": False,
            "checks": [],
            "warnings": [],
            "errors": [{"check": "intent", "message": str(exc)}],
        }
    result["seconds"] = round(time.perf_counter() - started, 4)
    return result


@app.post("/api/propose")
def propose(body: Proposal, portal_session: str | None = Cookie(default=None)):
    """Validate a proposed change and show exactly what it would alter."""
    require_session(portal_session)
    proposed_text, proposed = build(body.operation)

    validation = check(proposed)
    if not validation["ok"]:
        return {"validation": validation, "changes": [], "count": 0}

    try:
        proposed_files = render_all(proposed)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not render: {exc}") from exc

    current_files = render_all(load_intent(INTENT))
    current_files["intent/network.yml"] = INTENT.read_text()
    proposed_files["intent/network.yml"] = proposed_text

    changes = []
    for path in sorted(set(current_files) | set(proposed_files)):
        before = current_files.get(path, "")
        after = proposed_files.get(path, "")
        if before == after:
            continue
        changes.append(
            {
                "path": path,
                "status": "added" if not before else ("removed" if not after else "modified"),
                "diff": "\n".join(
                    difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        lineterm="",
                        n=3,
                    )
                ),
            }
        )

    return {"validation": validation, "changes": changes, "count": len(changes)}


def branch_name(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "network-change"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"portal/{slug}-{stamp}"


@app.post("/api/submit")
def submit(body: Proposal, portal_session: str | None = Cookie(default=None)):
    require_session(portal_session)
    proposed_text, proposed = build(body.operation)

    # Refuse to open a pull request for a change already known to be
    # broken. The pipeline would catch it, but spending a reviewer's
    # attention on it is its own cost. Checked before anything else, so
    # the answer never depends on how the portal happens to be set up.
    validation = check(proposed)
    if not validation["ok"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "This change does not pass validation, so it was not submitted.",
                "errors": validation["errors"],
            },
        )

    settings = Settings.from_env()
    if settings is None:
        raise HTTPException(
            status_code=503,
            detail="GitHub is not configured. Set GITHUB_TOKEN and GITHUB_REPO.",
        )

    title = body.title.strip() or "Network change proposed from the portal"
    files = render_all(proposed)
    files["intent/network.yml"] = proposed_text

    described = body.description.strip() or "_No description provided._"
    changed = "\n".join(f"- `{path}`" for path in sorted(files))
    body_text = (
        f"{described}\n\n"
        f"Proposed through the NetDevOps portal.\n\n"
        f"**Files in this change**\n{changed}\n\n"
        f"Validation passed before submission. The pipeline will build the "
        f"topology and run the full test suite before this can be merged."
    )

    try:
        result = GitHub(settings).propose(
            branch=branch_name(title), title=title, body=body_text, files=files
        )
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


# ---------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

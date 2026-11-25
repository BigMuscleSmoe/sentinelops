"""The remediation flow's GitHub client: the App's identity, signed in KMS, and a fixed list of endpoints with no merge.

The App (BUILD_PLAN.md, Phase 4 design, "GitHub"): Contents and Pull requests
read and write, Checks and Metadata read, installed on this repository only.
Its private key is in KMS (alias/sentinelops-github-app), imported once by
hand and never readable: the App's JWT is signed with kms:Sign (RS256), so
there's no key, JWT library, or crypto library here. An installation token is
asked for scoped to this repository and those four permissions, and refused
(and revoked) if GitHub grants anything else.

Every call names an endpoint in ENDPOINTS, a fixed list a test pins: no merge
(pulls/N/merge, POST merges), no repository_dispatch, no workflow, admin, or
settings write, no ref update or delete. Parameters are checked before any URL
is built: a branch is main, a release branch from the release map, or a fix
branch (sentinelops/fix-<version>-<module>); a file is one of checkout/'s
modules; a write goes only to a fix branch, and a PR only from a fix branch
into a release branch, as a draft. The rulesets (main and release/** locked,
everything but sentinelops/** closed to the App) are the wall; this is the
fence in front of it.

Retries (3, with backoff, then Unavailable; the flow records failed): a rate
limit (429, or 403 with GitHub's rate-limit headers) means the request didn't
run, so every endpoint retries it. A 5xx, a timeout, or a lost connection
leaves it unknown whether a write landed, so only endpoints safe to send twice
retry those; a create raises Unavailable at once, and the flow's step, which
looks for its own marker before every write, takes it from there.

Tokens and JWTs are never logged, printed, stored, or checkpointed: each use
asks for a fresh token when the last is within 5 minutes of expiry (they last
an hour), and revoke() ends one early. Redirects aren't followed, so the
Authorization header goes only to api.github.com.
"""

import base64
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sentinelops_core import code
from sentinelops_core.releases import RELEASES

API = "https://api.github.com"
API_VERSION = "2022-11-28"
OWNER = "BigMuscleSmoe"
NAME = "sentinelops"
REPO = f"{OWNER}/{NAME}"
USER_AGENT = "sentinelops-remediation"
KEY_ALIAS = "alias/sentinelops-github-app"
SIGNING_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"  # RS256
# What the App holds and every token asks for. GitHub may leave metadata out of
# a token's grant (every token can read it); anything else back is refused.
PERMISSIONS = {"checks": "read", "contents": "write", "metadata": "read", "pull_requests": "write"}
JWT_BACKDATE_S = 60  # GitHub's advice, for clock drift
JWT_LIFETIME_S = 540  # 9 minutes from now; GitHub's limit is 10
TOKEN_MARGIN_S = 300
TIMEOUT_S = 10
RETRIES = 3
MAX_WAIT_S = 60
MAX_RESPONSE_BYTES = 2_000_000

FIX_BRANCH = re.compile(r"sentinelops/fix-\d+\.\d+\.\d+-[a-z_][a-z0-9_]*", re.ASCII)
RELEASE_BRANCHES = frozenset(r.branch for r in RELEASES.values() if r.branch is not None)
_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,100}", re.ASCII)
_CLIENT_ID = re.compile(r"[A-Za-z0-9.]{1,64}", re.ASCII)
_CHECK_NAME = re.compile(r"[A-Za-z0-9 ()/_.-]{1,100}", re.ASCII)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # {repo} is always REPO; every other {name} is checked by _param
    auth: str  # "app": the App's JWT; "installation": an installation token
    repeatable: bool  # safe to send again after a 5xx or a lost connection
    query: frozenset[str] = frozenset()  # the query parameters it may take


ENDPOINTS: dict[str, Endpoint] = {
    # As the App, with its JWT.
    "app": Endpoint("GET", "/app", "app", True),
    "app_installations": Endpoint("GET", "/app/installations", "app", True, frozenset({"per_page"})),
    "installation_token": Endpoint("POST", "/app/installations/{installation_id}/access_tokens", "app", True),
    # As the installation, with a token scoped to this repository.
    "revoke_token": Endpoint("DELETE", "/installation/token", "installation", True),
    "installation_repositories": Endpoint("GET", "/installation/repositories", "installation", True, frozenset({"per_page"})),
    "repository": Endpoint("GET", "/repos/{repo}", "installation", True),
    "branch_rules": Endpoint("GET", "/repos/{repo}/rules/branches/{branch}", "installation", True),
    "get_ref": Endpoint("GET", "/repos/{repo}/git/ref/heads/{branch}", "installation", True),
    "create_ref": Endpoint("POST", "/repos/{repo}/git/refs", "installation", False),
    "get_file": Endpoint("GET", "/repos/{repo}/contents/{path}", "installation", True, frozenset({"ref"})),
    "put_file": Endpoint("PUT", "/repos/{repo}/contents/{path}", "installation", False),
    "list_pulls": Endpoint("GET", "/repos/{repo}/pulls", "installation", True, frozenset({"head", "base", "state"})),
    "create_pull": Endpoint("POST", "/repos/{repo}/pulls", "installation", False),
    "list_comments": Endpoint(
        "GET", "/repos/{repo}/issues/{number}/comments", "installation", True, frozenset({"per_page", "page"})
    ),
    "create_comment": Endpoint("POST", "/repos/{repo}/issues/{number}/comments", "installation", False),
    "check_runs": Endpoint(
        "GET", "/repos/{repo}/commits/{sha}/check-runs", "installation", True, frozenset({"check_name", "per_page"})
    ),
    # GraphQL only for what REST can't do, a draft PR made ready: one fixed
    # mutation, never a caller's query (GraphQL can merge too).
    "ready_for_review": Endpoint("POST", "/graphql", "installation", True),
}
# Sent only by their own methods, never through call().
_INTERNAL = frozenset({"installation_token", "revoke_token", "ready_for_review"})
READY_FOR_REVIEW = (
    "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
)


class NotAllowed(ValueError):
    """A call the client won't make: an endpoint off the list, or a parameter or body outside its rules."""


class GitHubError(Exception):
    def __init__(self, endpoint: str, status: int, message: str = "") -> None:
        self.endpoint, self.status = endpoint, status
        super().__init__(f"{endpoint}: HTTP {status}" + (f" ({message})" if message else ""))


class Refused(GitHubError):
    """GitHub said no: a 4xx other than a rate limit, or a GraphQL error. Not retried."""


class Unavailable(GitHubError):
    """No usable answer: a 5xx, a rate limit, or no response, after the retries (or at once, for a create)."""


class WrongGrant(GitHubError):
    """The installation token's grant wasn't exactly this repository and the App's permissions. It was revoked."""


class SigningFailed(Exception):
    """kms:Sign failed. Carries the error code only: AWS's messages can name an ARN."""


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # names in lower case
    body: bytes


class TransportError(Exception):
    """No response at all: a timeout or a lost connection."""


Transport = Callable[[Request], Response]


class Signer(Protocol):
    """What the client needs of boto3's KMS client."""

    def sign(self, **kwargs: Any) -> Mapping[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a 3xx then comes back as an HTTPError, answered as a refusal


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(request: Request) -> Response:
    """One HTTPS request to api.github.com with the standard library: no redirects, a 10 s timeout, 2 MB at most."""
    req = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers), method=request.method)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as reply:
            return Response(reply.status, _lower(reply.headers.items()), _read(reply))
    except urllib.error.HTTPError as error:
        with error:
            return Response(error.code, _lower(error.headers.items()), _read(error))
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
        failure = type(error).__name__
    raise TransportError(failure)


def encoded(authorization: str | None, body: Mapping[str, Any] | None = None) -> tuple[dict[str, str], bytes | None]:
    """The headers every request carries, and its JSON body: Authorization only when given, never logged."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
    if authorization is not None:
        headers["Authorization"] = authorization
    if body is None:
        return headers, None
    headers["Content-Type"] = "application/json"
    return headers, json.dumps(body, separators=(",", ":")).encode("utf-8")


def _lower(items: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


def _read(reply: Any) -> bytes:
    body = reply.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise TransportError("response over 2 MB")
    return body


def url(name: str, params: Mapping[str, object] | None = None, query: Mapping[str, object] | None = None) -> str:
    """The URL for one listed endpoint, every parameter checked first. NotAllowed if any fails."""
    endpoint = ENDPOINTS.get(name)
    if endpoint is None:
        raise NotAllowed(f"{name!r} isn't on the client's list")
    params = dict(params or {})
    wanted = set(_PLACEHOLDER.findall(endpoint.path)) - {"repo"}
    if set(params) != wanted:
        raise NotAllowed(f"{name} takes {sorted(wanted)}, not {sorted(params)}")
    path = _PLACEHOLDER.sub(lambda m: REPO if m[1] == "repo" else _param(m[1], params[m[1]]), endpoint.path)
    query = dict(query or {})
    if not set(query) <= endpoint.query:
        raise NotAllowed(f"{name} takes no query {sorted(set(query) - endpoint.query)}")
    encoded = urllib.parse.urlencode({key: _query(key, value) for key, value in sorted(query.items())})
    return API + path + (f"?{encoded}" if encoded else "")


def _param(name: str, value: object) -> str:
    if name == "branch" and isinstance(value, str) and _branch(value):
        return value
    if name == "path" and isinstance(value, str) and _repo_path(value):
        return value
    if name in ("number", "installation_id") and type(value) is int and 0 < value < 10**12:
        return str(value)
    if name == "sha" and isinstance(value, str) and _SHA.fullmatch(value):
        return value
    raise NotAllowed(f"{name} {value!r} isn't one the client sends")


def _query(name: str, value: object) -> str:
    ok = False
    if name == "ref":
        ok = isinstance(value, str) and (_SHA.fullmatch(value) is not None or _branch(value))
    elif name == "head":  # owner:branch, as GitHub takes a PR's head
        owner, _, branch = value.partition(":") if isinstance(value, str) else ("", "", "")
        ok = owner == OWNER and FIX_BRANCH.fullmatch(branch) is not None
    elif name == "base":
        ok = value in RELEASE_BRANCHES
    elif name == "state":
        ok = value in ("open", "closed", "all")
    elif name in ("per_page", "page"):
        ok = type(value) is int and 0 < value <= 100
    elif name == "check_name":
        ok = isinstance(value, str) and _CHECK_NAME.fullmatch(value) is not None
    if not ok:
        raise NotAllowed(f"query {name}={value!r} isn't one the client sends")
    return str(value)


def _branch(name: str) -> bool:
    """main, a release branch from the release map, or a fix branch: nothing else is ever named."""
    return name == "main" or name in RELEASE_BRANCHES or FIX_BRANCH.fullmatch(name) is not None


def _repo_path(path: str) -> bool:
    prefix = f"{code.SERVICE_ROOT}/"
    return path.startswith(prefix) and code.readable(path[len(prefix) :])


_PULL_FIELDS = frozenset({"title", "body", "head", "base", "draft", "maintainer_can_modify"})


def _check_body(name: str, body: Mapping[str, Any] | None) -> None:
    """A write's body: only to a fix branch, and a PR only from one into a release branch, as a draft."""
    if name == "create_ref":
        ok = (
            body is not None
            and set(body) == {"ref", "sha"}
            and isinstance(body["ref"], str)
            and body["ref"].startswith("refs/heads/")
            and FIX_BRANCH.fullmatch(body["ref"][len("refs/heads/") :]) is not None
            and isinstance(body["sha"], str)
            and _SHA.fullmatch(body["sha"]) is not None
        )
    elif name == "put_file":
        ok = (
            body is not None
            and {"message", "content", "branch"} <= set(body) <= {"message", "content", "branch", "sha"}
            and isinstance(body["branch"], str)
            and FIX_BRANCH.fullmatch(body["branch"]) is not None
        )
    elif name == "create_pull":
        ok = (
            body is not None
            and {"title", "body", "head", "base", "draft"} <= set(body) <= _PULL_FIELDS
            and isinstance(body["head"], str)
            and FIX_BRANCH.fullmatch(body["head"]) is not None
            and body["base"] in RELEASE_BRANCHES
            and body["draft"] is True
        )
    elif name == "create_comment":
        ok = body is not None and set(body) == {"body"} and isinstance(body["body"], str)
    else:
        ok = body is None
    if not ok:
        raise NotAllowed(f"{name}: a body outside its rules")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _epoch(stamp: object) -> float:
    if not isinstance(stamp, str):
        raise ValueError("not a time")
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a time without its zone")
    return moment.timestamp()


def _message(body: bytes) -> str:
    """GitHub's short message for a refusal, if it gave one: ASCII, 200 characters at most."""
    try:
        message = json.loads(body).get("message", "")
    except (ValueError, AttributeError):
        return ""
    return message.encode("ascii", "replace").decode("ascii")[:200] if isinstance(message, str) else ""


class GitHubApp:
    """The App, for this repository only."""

    def __init__(
        self,
        client_id: str,
        installation_id: int,
        kms: Signer,
        *,
        key_id: str = KEY_ALIAS,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
            raise ValueError("client_id isn't a GitHub App client ID")
        if type(installation_id) is not int or installation_id <= 0:
            raise ValueError("installation_id isn't an installation ID")
        self._client_id, self._installation_id, self._kms, self._key_id = client_id, installation_id, kms, key_id
        self._transport, self._clock, self._sleep = transport, clock, sleep
        self._token: str | None = None
        self._token_expires = 0.0

    def __repr__(self) -> str:
        return f"GitHubApp(repo={REPO!r}, token={'held' if self._token else 'none'})"

    # --- the App's identity -------------------------------------------------

    def app_jwt(self) -> str:
        """The App's JWT, signed by KMS: RS256, issued by the client ID, 60 s back, 9 minutes on."""
        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {"iat": now - JWT_BACKDATE_S, "exp": now + JWT_LIFETIME_S, "iss": self._client_id}
        signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":")).encode("ascii")) for part in (header, claims))
        failure = None
        try:
            signed = self._kms.sign(
                KeyId=self._key_id,
                Message=signing_input.encode("ascii"),
                MessageType="RAW",
                SigningAlgorithm=SIGNING_ALGORITHM,
            )
        except Exception as error:  # botocore's ClientError, or no credentials
            failure = getattr(error, "response", {}).get("Error", {}).get("Code") or type(error).__name__
        if failure is not None:
            raise SigningFailed(f"kms:Sign failed ({failure})")
        return f"{signing_input}.{_b64url(signed['Signature'])}"

    def _installation_token(self) -> str:
        if self._token is not None and self._clock() < self._token_expires - TOKEN_MARGIN_S:
            return self._token
        body = {"repositories": [NAME], "permissions": PERMISSIONS}
        data = self._send(
            "installation_token",
            url("installation_token", {"installation_id": self._installation_id}),
            f"Bearer {self.app_jwt()}",
            body,
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise Refused("installation_token", 201, "no token in the answer")
        granted = data.get("permissions")
        repositories = [r.get("full_name") for r in data.get("repositories") or [] if isinstance(r, dict)]
        exact = granted in (PERMISSIONS, {k: v for k, v in PERMISSIONS.items() if k != "metadata"})
        try:
            expires = _epoch(data.get("expires_at"))
        except ValueError:
            expires = None
        if not exact or data.get("repository_selection") != "selected" or repositories != [REPO] or expires is None:
            self._revoke(token)
            raise WrongGrant("installation_token", 201, "the grant isn't exactly this repository and the four permissions")
        self._token, self._token_expires = token, expires
        return token

    def revoke(self) -> None:
        """End the token now, if one is held."""
        if self._token is not None:
            token, self._token = self._token, None
            self._revoke(token)

    def _revoke(self, token: str) -> None:
        self._send("revoke_token", url("revoke_token"), f"Bearer {token}", None)

    # --- calls ----------------------------------------------------------------

    def call(
        self,
        name: str,
        params: Mapping[str, object] | None = None,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """One listed endpoint, its parameters and body checked first: the answer's JSON, or None for a 204."""
        if name in _INTERNAL:
            raise NotAllowed(f"{name} is sent only by its own method")
        target = url(name, params, query)
        _check_body(name, body)
        endpoint = ENDPOINTS[name]
        auth = self.app_jwt() if endpoint.auth == "app" else self._installation_token()
        return self._send(name, target, f"Bearer {auth}", body)

    def mark_ready_for_review(self, node_id: str) -> None:
        """Turn a draft PR, by its GraphQL node ID, into one ready for review: the one GraphQL call there is."""
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            raise NotAllowed(f"node ID {node_id!r} isn't one the client sends")
        body = {"query": READY_FOR_REVIEW, "variables": {"id": node_id}}
        data = self._send("ready_for_review", url("ready_for_review"), f"Bearer {self._installation_token()}", body)
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
            raise Refused("ready_for_review", 200, "GraphQL refused it")

    def _send(self, name: str, target: str, authorization: str, body: Mapping[str, Any] | None) -> Any:
        endpoint = ENDPOINTS[name]
        request = Request(endpoint.method, target, *encoded(authorization, body))
        for attempt in range(RETRIES + 1):
            try:
                response = self._transport(request)
            except TransportError:
                status, wait, again = 0, None, endpoint.repeatable
            else:
                status = response.status
                if 200 <= status < 300:
                    if not response.body.strip():
                        return None
                    try:
                        return json.loads(response.body)
                    except ValueError:
                        pass
                    raise Refused(name, status, "an answer that isn't JSON")
                limited = status == 429 or (status == 403 and _rate_limited(response.headers))
                if not limited and status < 500:
                    raise Refused(name, status, _message(response.body))
                wait = _wait(response.headers, self._clock()) if limited else None
                again = limited or endpoint.repeatable
            if not again or attempt == RETRIES:
                raise Unavailable(name, status, "no response" if status == 0 else "")
            self._sleep(wait if wait is not None else float(2**attempt))
        raise AssertionError("unreachable")

    # --- what the validation step reads --------------------------------------------

    def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        """The active rules on a branch, from every ruleset that targets it."""
        rules = self.call("branch_rules", {"branch": branch})
        if not isinstance(rules, list):
            raise Refused("branch_rules", 200, "not a list of rules")
        return [rule for rule in rules if isinstance(rule, dict)]

    def head_commit(self, branch: str) -> str:
        """The commit a branch points at."""
        ref = self.call("get_ref", {"branch": branch})
        if not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}":
            raise Refused("get_ref", 200, "not that branch")
        target = ref.get("object")
        sha = target.get("sha") if isinstance(target, dict) and target.get("type") == "commit" else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Refused("get_ref", 200, "not the branch's commit")
        return sha

    def file_at(self, path: str, ref: str) -> bytes:
        """A file's bytes at a commit."""
        found = self.call("get_file", {"path": path}, {"ref": ref})
        if not isinstance(found, dict) or found.get("type") != "file" or found.get("path") != path:
            raise Refused("get_file", 200, "not that file")
        if found.get("encoding") != "base64" or not isinstance(found.get("content"), str):
            raise Refused("get_file", 200, "no content in the answer")
        return base64.b64decode("".join(found["content"].split()), validate=True)


def _rate_limited(headers: Mapping[str, str]) -> bool:
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


def _wait(headers: Mapping[str, str], now: float) -> float | None:
    """How long GitHub asked to wait, at most a minute: retry-after's seconds, or until the limit resets."""
    try:
        if "retry-after" in headers:
            return float(min(max(int(headers["retry-after"]), 1), MAX_WAIT_S))
        if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
            return float(min(max(int(headers["x-ratelimit-reset"]) - int(now), 1), MAX_WAIT_S))
    except ValueError:
        pass
    return None

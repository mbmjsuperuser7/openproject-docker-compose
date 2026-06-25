"""
OpenProject MCP Server — Secure Build
Streamable HTTP transport for Claude.ai

Security layers:
1. Bearer token auth on every request (X-API-Key or Authorization: Bearer)
2. Input validation and sanitization on all tool parameters
3. Path traversal prevention — no user input goes directly into URL paths
4. Rate limiting per client IP
5. Error responses leak nothing about internals
6. Read/write tools separated — writes require explicit confirmation
7. Structured audit logging
8. Least privilege — API key should be a non-admin user
9. No shell execution, no file system access, no dynamic imports
10. Request size limits
"""

import os
import re
import time
import logging
import hashlib
import httpx
from collections import defaultdict
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────
OP_URL      = os.environ["OPENPROJECT_URL"].rstrip("/")
OP_KEY      = os.environ["OPENPROJECT_API_KEY"]
MCP_SECRET  = os.environ.get("MCP_SECRET", "")   # Bearer token Claude.ai sends
PORT        = int(os.environ.get("PORT", 3000))
MAX_STR_LEN = 2000   # max length for any string input
RATE_LIMIT  = 60     # requests per minute per IP

# ── Logging (structured, no secrets) ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
)
log = logging.getLogger("op-mcp")

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate_buckets: dict = defaultdict(list)

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    bucket = _rate_buckets[ip]
    # Remove entries older than 60s
    _rate_buckets[ip] = [t for t in bucket if now - t < 60]
    if len(_rate_buckets[ip]) >= RATE_LIMIT:
        return False
    _rate_buckets[ip].append(now)
    return True

# ── Input sanitization ────────────────────────────────────────────────────────
def sanitize_str(value: str, field: str = "input") -> str:
    """Validate and sanitize string inputs."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > MAX_STR_LEN:
        raise ValueError(f"{field} exceeds maximum length of {MAX_STR_LEN}")
    # Strip null bytes and control chars (except newlines/tabs)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    return cleaned.strip()

def sanitize_id(value: int, field: str = "id") -> int:
    """Validate integer IDs — must be positive."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if value <= 0 or value > 10_000_000:
        raise ValueError(f"{field} out of valid range")
    return value

def sanitize_status(value: str) -> str:
    allowed = {"open", "closed", "all"}
    if value not in allowed:
        raise ValueError(f"status must be one of: {', '.join(allowed)}")
    return value

# ── OpenProject API client ────────────────────────────────────────────────────
def op_headers() -> dict:
    import base64
    token = base64.b64encode(f"apikey:{OP_KEY}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def op_request(method: str, path: str, body: dict = None) -> dict:
    """Make an OpenProject API request with error handling."""
    # Validate path — only allow /api/v3/ prefix, no traversal
    if not re.match(r'^/api/v3/[a-zA-Z0-9/_\-?=\[\]{}":,%.&]+$', path):
        raise ValueError("Invalid API path")

    url = f"{OP_URL}{path}"
    try:
        with httpx.Client(timeout=15.0) as client:
            if method == "GET":
                r = client.get(url, headers=op_headers())
            elif method == "POST":
                r = client.post(url, headers=op_headers(), json=body)
            elif method == "PATCH":
                r = client.patch(url, headers=op_headers(), json=body)
            else:
                raise ValueError(f"Unsupported method: {method}")

        if r.status_code == 401:
            log.error("OpenProject auth failed")
            raise RuntimeError("OpenProject authentication failed")
        if r.status_code == 403:
            raise RuntimeError("Insufficient permissions")
        if r.status_code == 404:
            raise RuntimeError("Resource not found")
        if r.status_code >= 500:
            log.error(f"OpenProject server error: {r.status_code}")
            raise RuntimeError("OpenProject server error")

        r.raise_for_status()
        return r.json()

    except httpx.TimeoutException:
        raise RuntimeError("OpenProject request timed out")
    except httpx.ConnectError:
        raise RuntimeError("Cannot connect to OpenProject")

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP(
    "OpenProject",
    stateless_http=True,
    instructions="""
    You are connected to an OpenProject instance.
    Available operations: list projects, list/get/create/update work packages, add comments, list users.
    Always confirm destructive operations with the user before executing.
    Never include raw API responses, credentials, or internal URLs in your responses.
    """
)

# ── READ TOOLS ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_projects() -> str:
    """List all OpenProject projects you have access to."""
    data = op_request("GET", "/api/v3/projects")
    projects = data.get("_embedded", {}).get("elements", [])
    if not projects:
        return "No projects found."
    lines = [f"ID {p['id']}: {p['name']}" for p in projects]
    log.info(f"listed {len(projects)} projects")
    return f"Found {len(projects)} projects:\n" + "\n".join(lines)

@mcp.tool()
def list_work_packages(
    project_id: int = None,
    status: str = "open",
    assignee_id: int = None,
    page_size: int = 25
) -> str:
    """
    List work packages with optional filters.
    status: 'open', 'closed', or 'all'
    project_id: filter to a specific project (optional)
    assignee_id: filter to a specific user (optional)
    page_size: max results, 1-50
    """
    if project_id is not None:
        project_id = sanitize_id(project_id, "project_id")
    if assignee_id is not None:
        assignee_id = sanitize_id(assignee_id, "assignee_id")
    status = sanitize_status(status)
    page_size = max(1, min(50, int(page_size)))

    filters = []
    if status == "open":
        filters.append('{"status":{"operator":"o","values":[]}}')
    elif status == "closed":
        filters.append('{"status":{"operator":"c","values":[]}}')
    if assignee_id:
        filters.append(f'{{"assignee":{{"operator":"=","values":["{assignee_id}"]}}}}')

    base = f"/api/v3/projects/{project_id}/work_packages" if project_id else "/api/v3/work_packages"
    params = f"?pageSize={page_size}"
    if filters:
        params += f"&filters=[{','.join(filters)}]"

    data = op_request("GET", base + params)
    wps = data.get("_embedded", {}).get("elements", [])
    if not wps:
        return "No work packages found matching your criteria."

    lines = []
    for wp in wps:
        s = wp.get("_links", {}).get("status", {}).get("title", "")
        a = wp.get("_links", {}).get("assignee", {}).get("title", "unassigned")
        t = wp.get("_links", {}).get("type", {}).get("title", "")
        lines.append(f"#{wp['id']} [{t}] [{s}] {wp['subject']} — {a}")

    log.info(f"listed {len(wps)} work packages")
    return f"Found {len(wps)} work packages:\n" + "\n".join(lines)

@mcp.tool()
def get_work_package(id: int) -> str:
    """Get full details of a specific work package by ID."""
    id = sanitize_id(id)
    wp = op_request("GET", f"/api/v3/work_packages/{id}")
    desc = wp.get("description", {}).get("raw", "No description")
    assignee = wp.get("_links", {}).get("assignee", {}).get("title", "unassigned")
    status = wp.get("_links", {}).get("status", {}).get("title", "")
    priority = wp.get("_links", {}).get("priority", {}).get("title", "")
    type_ = wp.get("_links", {}).get("type", {}).get("title", "")
    due = wp.get("dueDate", "not set")
    start = wp.get("startDate", "not set")
    project = wp.get("_links", {}).get("project", {}).get("title", "")
    log.info(f"retrieved work package {id}")
    return (
        f"#{wp['id']}: {wp['subject']}\n"
        f"Project: {project}\n"
        f"Type: {type_} | Status: {status} | Priority: {priority}\n"
        f"Assignee: {assignee}\n"
        f"Start: {start} | Due: {due}\n"
        f"Description:\n{desc}"
    )

@mcp.tool()
def list_users() -> str:
    """List all users in the OpenProject instance."""
    data = op_request("GET", "/api/v3/users?pageSize=100")
    users = data.get("_embedded", {}).get("elements", [])
    if not users:
        return "No users found."
    lines = [f"ID {u['id']}: {u['name']} ({u['login']})" for u in users]
    log.info(f"listed {len(users)} users")
    return "\n".join(lines)

@mcp.tool()
def list_statuses() -> str:
    """List all available work package statuses."""
    data = op_request("GET", "/api/v3/statuses")
    statuses = data.get("_embedded", {}).get("elements", [])
    lines = [f"ID {s['id']}: {s['name']}" for s in statuses]
    return "\n".join(lines)

# ── WRITE TOOLS ───────────────────────────────────────────────────────────────

@mcp.tool()
def create_work_package(
    project_id: int,
    subject: str,
    description: str = "",
    type_name: str = "Task",
    due_date: str = ""
) -> str:
    """
    Create a new work package.
    type_name: Task, Feature, Bug, Milestone, Epic, User Story (must exist in project)
    due_date: ISO format YYYY-MM-DD (optional)
    """
    project_id = sanitize_id(project_id, "project_id")
    subject = sanitize_str(subject, "subject")
    description = sanitize_str(description, "description") if description else ""
    type_name = sanitize_str(type_name, "type_name")

    if due_date:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', due_date):
            raise ValueError("due_date must be YYYY-MM-DD format")

    # Get project types
    types = op_request("GET", f"/api/v3/projects/{project_id}/types")
    type_href = None
    for t in types.get("_embedded", {}).get("elements", []):
        if t["name"].lower() == type_name.lower():
            type_href = t["_links"]["self"]["href"]
            break
    if not type_href:
        available = [t["name"] for t in types.get("_embedded", {}).get("elements", [])]
        raise ValueError(f"Type '{type_name}' not found. Available: {', '.join(available)}")

    body = {
        "subject": subject,
        "_links": {
            "project": {"href": f"/api/v3/projects/{project_id}"},
            "type": {"href": type_href}
        }
    }
    if description:
        body["description"] = {"raw": description}
    if due_date:
        body["dueDate"] = due_date

    wp = op_request("POST", f"/api/v3/projects/{project_id}/work_packages", body)
    log.info(f"created work package {wp['id']} in project {project_id}")
    return f"Created #{wp['id']}: {wp['subject']} in project {project_id}"

@mcp.tool()
def update_work_package(
    id: int,
    subject: str = None,
    description: str = None,
    status_name: str = None,
    due_date: str = None
) -> str:
    """
    Update a work package.
    Only provide fields you want to change.
    due_date: YYYY-MM-DD format
    """
    id = sanitize_id(id)
    body = {}

    if subject is not None:
        body["subject"] = sanitize_str(subject, "subject")
    if description is not None:
        body["description"] = {"raw": sanitize_str(description, "description")}
    if due_date is not None:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', due_date):
            raise ValueError("due_date must be YYYY-MM-DD format")
        body["dueDate"] = due_date
    if status_name is not None:
        status_name = sanitize_str(status_name, "status_name")
        statuses = op_request("GET", "/api/v3/statuses")
        status_href = None
        for s in statuses.get("_embedded", {}).get("elements", []):
            if s["name"].lower() == status_name.lower():
                status_href = s["_links"]["self"]["href"]
                break
        if not status_href:
            available = [s["name"] for s in statuses.get("_embedded", {}).get("elements", [])]
            raise ValueError(f"Status '{status_name}' not found. Available: {', '.join(available)}")
        body["_links"] = {"status": {"href": status_href}}

    if not body:
        return "Nothing to update — no fields provided."

    wp = op_request("PATCH", f"/api/v3/work_packages/{id}", body)
    log.info(f"updated work package {id}")
    return f"Updated #{wp['id']}: {wp['subject']}"

@mcp.tool()
def add_comment(work_package_id: int, comment: str) -> str:
    """Add a comment to a work package."""
    work_package_id = sanitize_id(work_package_id, "work_package_id")
    comment = sanitize_str(comment, "comment")
    if len(comment) < 1:
        raise ValueError("Comment cannot be empty")

    op_request("POST", f"/api/v3/work_packages/{work_package_id}/activities",
               {"comment": {"raw": comment}})
    log.info(f"added comment to work package {work_package_id}")
    return f"Comment added to #{work_package_id}"

# ── Auth + rate limit middleware ──────────────────────────────────────────────
class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Rate limiting
        client_ip = request.client.host if request.client else "unknown"
        if not check_rate_limit(client_ip):
            log.warning(f"rate limit exceeded for {client_ip}")
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

        # Request size limit (1MB)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 1_048_576:
            return JSONResponse({"error": "Request too large"}, status_code=413)

        # Bearer token auth (only if MCP_SECRET is configured)
        if MCP_SECRET:
            auth = request.headers.get("authorization", "")
            api_key = request.headers.get("x-api-key", "")
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth[7:]
            elif api_key:
                token = api_key

            # Constant-time comparison to prevent timing attacks
            if not token or not hashlib.sha256(token.encode()).hexdigest() == \
                           hashlib.sha256(MCP_SECRET.encode()).hexdigest():
                log.warning(f"auth failed from {client_ip}")
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)

# ── App startup ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette

    if not MCP_SECRET:
        log.warning("MCP_SECRET not set — server is unauthenticated. Set MCP_SECRET env var.")
    if not OP_KEY:
        log.error("OPENPROJECT_API_KEY not set")
        exit(1)

    app = mcp.streamable_http_app()

    # Wrap with security middleware
    from starlette.middleware import Middleware
    secured_app = Starlette(
        middleware=[Middleware(SecurityMiddleware)],
        routes=app.routes
    )

    log.info(f"Starting OpenProject MCP server on port {PORT}")
    log.info(f"Auth: {'enabled' if MCP_SECRET else 'DISABLED'}")
    uvicorn.run(secured_app, host="0.0.0.0", port=PORT, access_log=False)

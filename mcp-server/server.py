"""
OpenProject MCP Server
Streamable HTTP transport for Claude.ai
"""
import os
import httpx
from mcp.server.fastmcp import FastMCP

OP_URL = os.environ["OPENPROJECT_URL"].rstrip("/")
OP_KEY = os.environ["OPENPROJECT_API_KEY"]

mcp = FastMCP("OpenProject", stateless_http=True)

def op_headers():
    import base64
    token = base64.b64encode(f"apikey:{OP_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def op_get(path: str) -> dict:
    r = httpx.get(f"{OP_URL}/api/v3{path}", headers=op_headers(), timeout=15)
    r.raise_for_status()
    return r.json()

def op_post(path: str, body: dict) -> dict:
    r = httpx.post(f"{OP_URL}/api/v3{path}", headers=op_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def op_patch(path: str, body: dict) -> dict:
    r = httpx.patch(f"{OP_URL}/api/v3{path}", headers=op_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()

@mcp.tool()
def list_projects() -> str:
    """List all OpenProject projects"""
    data = op_get("/projects")
    projects = data.get("_embedded", {}).get("elements", [])
    if not projects:
        return "No projects found."
    lines = [f"ID {p['id']}: {p['name']} — {p.get('status', {}).get('explanation', {}).get('raw', 'active')}" for p in projects]
    return "\n".join(lines)

@mcp.tool()
def list_work_packages(project_id: int = None, status: str = "open", assignee_id: int = None) -> str:
    """List work packages. status: 'open', 'closed', or 'all'. Optionally filter by project_id or assignee_id."""
    filters = []
    if status == "open":
        filters.append('{"status":{"operator":"o","values":[]}}')
    elif status == "closed":
        filters.append('{"status":{"operator":"c","values":[]}}')
    
    if assignee_id:
        filters.append(f'{{"assignee":{{"operator":"=","values":["{assignee_id}"]}}}}')

    path = f"/projects/{project_id}/work_packages" if project_id else "/work_packages"
    if filters:
        path += f"?filters=[{','.join(filters)}]"

    data = op_get(path)
    wps = data.get("_embedded", {}).get("elements", [])
    if not wps:
        return "No work packages found."
    
    lines = []
    for wp in wps:
        status_name = wp.get("status", {}).get("_links", {}).get("status", {}).get("title", "")
        assignee = wp.get("_links", {}).get("assignee", {}).get("title", "unassigned")
        lines.append(f"#{wp['id']} [{status_name}] {wp['subject']} — {assignee}")
    return "\n".join(lines)

@mcp.tool()
def get_work_package(id: int) -> str:
    """Get full details of a work package by ID"""
    wp = op_get(f"/work_packages/{id}")
    desc = wp.get("description", {}).get("raw", "No description")
    assignee = wp.get("_links", {}).get("assignee", {}).get("title", "unassigned")
    status = wp.get("_links", {}).get("status", {}).get("title", "")
    priority = wp.get("_links", {}).get("priority", {}).get("title", "")
    due = wp.get("dueDate", "not set")
    return f"#{wp['id']}: {wp['subject']}\nStatus: {status}\nPriority: {priority}\nAssignee: {assignee}\nDue: {due}\nDescription: {desc}"

@mcp.tool()
def create_work_package(project_id: int, subject: str, description: str = "", type_name: str = "Task") -> str:
    """Create a new work package in a project"""
    # Get project types to find the type ID
    types = op_get(f"/projects/{project_id}/types")
    type_href = None
    for t in types.get("_embedded", {}).get("elements", []):
        if t["name"].lower() == type_name.lower():
            type_href = t["_links"]["self"]["href"]
            break
    if not type_href:
        type_href = types.get("_embedded", {}).get("elements", [{}])[0].get("_links", {}).get("self", {}).get("href")

    body = {
        "subject": subject,
        "_links": {
            "project": {"href": f"/api/v3/projects/{project_id}"},
            "type": {"href": type_href}
        }
    }
    if description:
        body["description"] = {"raw": description}

    wp = op_post(f"/projects/{project_id}/work_packages", body)
    return f"Created #{wp['id']}: {wp['subject']}"

@mcp.tool()
def update_work_package(id: int, subject: str = None, description: str = None, status_name: str = None) -> str:
    """Update a work package — change subject, description, or status"""
    body = {}
    if subject:
        body["subject"] = subject
    if description:
        body["description"] = {"raw": description}
    if status_name:
        # Get available statuses
        statuses = op_get("/statuses")
        for s in statuses.get("_embedded", {}).get("elements", []):
            if s["name"].lower() == status_name.lower():
                body["_links"] = {"status": {"href": s["_links"]["self"]["href"]}}
                break

    wp = op_patch(f"/work_packages/{id}", body)
    return f"Updated #{wp['id']}: {wp['subject']}"

@mcp.tool()
def add_comment(work_package_id: int, comment: str) -> str:
    """Add a comment to a work package"""
    body = {"comment": {"raw": comment}}
    result = op_post(f"/work_packages/{work_package_id}/activities", body)
    return f"Comment added to #{work_package_id}"

@mcp.tool()
def list_users() -> str:
    """List all users in OpenProject"""
    data = op_get("/users")
    users = data.get("_embedded", {}).get("elements", [])
    if not users:
        return "No users found."
    lines = [f"ID {u['id']}: {u['name']} ({u['login']})" for u in users]
    return "\n".join(lines)

if __name__ == "__main__":
    import uvicorn
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

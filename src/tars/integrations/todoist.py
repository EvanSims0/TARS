"""Todoist (unified API v1): reminders, to-dos and the store-sectioned shopping list.

Tasks with a due time get Todoist's automatic due-time reminder on the phone,
which is free; custom reminder times would need Todoist Pro.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..actions import ActionResult, Tier, Tool, ToolError, schema
from ..config import TodoistConfig

BASE_URL = "https://api.todoist.com/api/v1"


class TodoistClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=10)
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, BASE_URL + path, headers=self._headers, **kwargs)
        except httpx.HTTPError as e:
            raise ToolError("Todoist isn't reachable right now.") from e
        if resp.status_code in (401, 403):
            raise ToolError("Todoist rejected the token; it may need reconnecting.")
        if resp.status_code >= 400:
            raise ToolError(f"Todoist returned an error ({resp.status_code}).")
        return resp.json() if resp.content else None

    async def _paged(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        items: list[dict[str, Any]] = []
        while True:
            page = await self._request("GET", path, params=params)
            if isinstance(page, list):  # tolerate unpaged responses
                return items + page
            items.extend(page.get("results", []))
            cursor = page.get("next_cursor")
            if not cursor or cursor == params.get("cursor"):
                return items
            params["cursor"] = cursor

    async def tasks(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            return await self._paged("/tasks/filter", {"query": query})
        return await self._paged("/tasks")

    async def add_task(self, **fields: Any) -> dict[str, Any]:
        return await self._request("POST", "/tasks", json={k: v for k, v in fields.items() if v})

    async def close_task(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/close")

    async def reopen_task(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/reopen")

    async def delete_task(self, task_id: str) -> None:
        await self._request("DELETE", f"/tasks/{task_id}")

    async def projects(self) -> list[dict[str, Any]]:
        return await self._paged("/projects")

    async def add_project(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/projects", json={"name": name})

    async def sections(self, project_id: str) -> list[dict[str, Any]]:
        return await self._paged("/sections", {"project_id": project_id})

    async def add_section(self, project_id: str, name: str) -> dict[str, Any]:
        return await self._request("POST", "/sections", json={"project_id": project_id, "name": name})


def _describe(task: dict[str, Any]) -> str:
    due = task.get("due") or {}
    when = due.get("string") or due.get("date") or ""
    return f"{task['content']}" + (f" (due {when})" if when else "") + f" [id {task['id']}]"


def build_tools(client: TodoistClient, config: TodoistConfig) -> list[Tool]:
    async def find_project(name: str, create: bool) -> dict[str, Any]:
        for project in await client.projects():
            if project["name"].lower() == name.lower():
                return project
        if not create:
            raise ToolError(f"There's no Todoist project called {name}.")
        return await client.add_project(name)

    async def find_section(project_id: str, name: str) -> dict[str, Any]:
        for section in await client.sections(project_id):
            if section["name"].lower() == name.lower():
                return section
        return await client.add_section(project_id, name)

    def undo_add(task_id: str, label: str):
        async def undo() -> str:
            await client.delete_task(task_id)
            return f"Removed {label}."
        return undo

    async def add_task(args: dict[str, Any]) -> ActionResult:
        task = await client.add_task(content=args["content"], due_string=args.get("due"))
        when = f", due {args['due']}" if args.get("due") else ""
        return ActionResult(
            f"Added '{task['content']}'{when}. [id {task['id']}]",
            undo=undo_add(task["id"], f"'{task['content']}'"),
            undo_label=f"add task '{task['content']}'",
        )

    async def list_tasks(args: dict[str, Any]) -> str:
        tasks = await client.tasks(args.get("filter") or "today | overdue")
        if not tasks:
            return "No matching tasks."
        return f"{len(tasks)} tasks:\n" + "\n".join(_describe(t) for t in tasks)

    async def complete_task(args: dict[str, Any]) -> ActionResult:
        await client.close_task(args["task_id"])

        async def undo() -> str:
            await client.reopen_task(args["task_id"])
            return "Reopened it."

        return ActionResult("Marked done.", undo=undo, undo_label="complete task")

    async def add_to_shopping(args: dict[str, Any]) -> ActionResult:
        project = await find_project(config.shopping_project, create=True)
        store = (args.get("store") or config.default_store).strip()
        section = await find_section(project["id"], store)
        added = []
        for item in args["items"]:
            task = await client.add_task(content=item, project_id=project["id"], section_id=section["id"])
            added.append(task)

        async def undo() -> str:
            for task in added:
                await client.delete_task(task["id"])
            return f"Took {len(added)} items back off the list."

        names = ", ".join(t["content"] for t in added)
        return ActionResult(f"Added {names} under {store}.", undo=undo, undo_label=f"add {names}")

    async def read_shopping(args: dict[str, Any]) -> str:
        project = await find_project(config.shopping_project, create=False)
        sections = {s["id"]: s["name"] for s in await client.sections(project["id"])}
        tasks = await client.tasks(f"#{config.shopping_project}")
        if not tasks:
            return "The shopping list is empty."
        by_store: dict[str, list[str]] = {}
        for t in tasks:
            store = sections.get(t.get("section_id") or "", "Unsorted")
            if args.get("store") and store.lower() != args["store"].lower():
                continue
            by_store.setdefault(store, []).append(f"{t['content']} [id {t['id']}]")
        return "\n".join(f"{store}: {', '.join(items)}" for store, items in by_store.items()) or "Nothing for that store."

    return [
        Tool(
            "add_reminder",
            "Create a to-do or reminder in Todoist. Give `due` in natural language with a time "
            "(e.g. 'Sunday 10am') when it should ping the phone at that moment.",
            schema({"content": {"type": "string"}, "due": {"type": "string"}}, ["content"]),
            Tier.CREATE_FOR_YOU, add_task, service="Todoist",
        ),
        Tool(
            "list_tasks",
            "List Todoist tasks. `filter` uses Todoist filter syntax, e.g. 'today | overdue', 'tomorrow', '7 days'.",
            schema({"filter": {"type": "string"}}),
            Tier.READ, list_tasks, cue="Checking your list.", service="Todoist",
        ),
        Tool(
            "complete_task",
            "Mark a Todoist task done by its id (find it with list_tasks first).",
            schema({"task_id": {"type": "string"}}, ["task_id"]),
            Tier.CREATE_FOR_YOU, complete_task, service="Todoist",
        ),
        Tool(
            "add_to_shopping_list",
            "Add items to the running shopping list, under the store they're bought at.",
            schema({"items": {"type": "array", "items": {"type": "string"}}, "store": {"type": "string"}}, ["items"]),
            Tier.CREATE_FOR_YOU, add_to_shopping, service="Todoist",
        ),
        Tool(
            "read_shopping_list",
            "Read the shopping list, grouped by store, optionally for one store. Tick items off with "
            "complete_task using their ids.",
            schema({"store": {"type": "string"}}),
            Tier.READ, read_shopping, service="Todoist",
        ),
    ]

"""Todoist (unified API v1): reminders, to-dos and the store-sectioned shopping list.

Tasks with a due time get Todoist's automatic due-time reminder on the phone,
which is free; custom reminder times would need Todoist Pro.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
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

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}", json=fields)

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


BLACK_HOLE_LABEL = "black-hole"


def _due_day(task: dict[str, Any]) -> date | None:
    raw = (task.get("due") or {}).get("date")
    if not raw:
        return None
    return date.fromisoformat(raw[:10])


class PostponeTracker:
    """Notices tasks that keep getting pushed back, so they can go into the black hole.

    Todoist doesn't record postponements, so TARS remembers each open task's due date
    and counts every time it moves later. Tasks long overdue count as well.
    """

    def __init__(self, path: Path, threshold: int = 3, overdue_days: int = 14):
        self.path = path
        self.threshold = threshold
        self.overdue_days = overdue_days

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def observe(self, tasks: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
        """Update counts from the full list of open tasks; return those past the event horizon."""
        today = today or date.today()
        seen = self._load()
        current: dict[str, dict[str, Any]] = {}
        flagged = []
        for task in tasks:
            due = _due_day(task)
            if due is None:
                continue
            entry = seen.get(task["id"], {"count": 0})
            if "due" in entry and due > date.fromisoformat(entry["due"]):
                entry["count"] += 1
            entry.update(due=due.isoformat(), content=task["content"])
            current[task["id"]] = entry
            overdue = (today - due).days
            if entry["count"] >= self.threshold or overdue >= self.overdue_days:
                flagged.append({**task, "postponed": entry["count"], "overdue_days": max(overdue, 0)})
        self._save(current)  # finished or deleted tasks drop out
        return flagged


def _describe(task: dict[str, Any]) -> str:
    due = task.get("due") or {}
    when = due.get("string") or due.get("date") or ""
    return f"{task['content']}" + (f" (due {when})" if when else "") + f" [id {task['id']}]"


async def scan_black_hole(client: TodoistClient, tracker: PostponeTracker) -> list[dict[str, Any]]:
    flagged = tracker.observe(await client.tasks())
    for task in flagged:
        labels = task.get("labels") or []
        if BLACK_HOLE_LABEL not in labels:
            await client.update_task(task["id"], labels=[*labels, BLACK_HOLE_LABEL])
    return flagged


async def open_due_today(client: TodoistClient) -> int:
    return len(await client.tasks("today"))


def build_tools(client: TodoistClient, config: TodoistConfig, tracker: PostponeTracker | None = None) -> list[Tool]:
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

    async def black_hole(args: dict[str, Any]) -> str:
        if tracker is None:
            return "The black hole isn't set up."
        flagged = await scan_black_hole(client, tracker)
        if not flagged:
            return "Nothing past the event horizon. Every task is moving."
        rows = [
            f"{t['content']} [id {t['id']}]: postponed {t['postponed']} times"
            + (f", {t['overdue_days']} days overdue" if t["overdue_days"] else "")
            for t in flagged
        ]
        return (f"{len(flagged)} tasks past the event horizon (labelled {BLACK_HOLE_LABEL} in Todoist):\n"
                + "\n".join(rows)
                + "\nOffer to reschedule each with a firm date, or drop it (mark it done; it can be reopened).")

    async def reschedule(args: dict[str, Any]) -> ActionResult:
        before = await client.get_task(args["task_id"])
        old_due = (before.get("due") or {}).get("date")
        labels = [label for label in before.get("labels") or [] if label != BLACK_HOLE_LABEL]
        task = await client.update_task(args["task_id"], due_string=args["due"], labels=labels)

        async def undo() -> str:
            if old_due and len(old_due) > 10:
                await client.update_task(args["task_id"], due_datetime=old_due)
            elif old_due:
                await client.update_task(args["task_id"], due_date=old_due)
            return "Put the old date back."

        return ActionResult(f"Rescheduled '{task.get('content', before.get('content'))}' to {args['due']}.",
                            undo=undo, undo_label="reschedule task")

    return [
        Tool(
            "black_hole",
            "Find tasks that keep getting postponed or are long overdue (the black hole), and label them.",
            schema({}), Tier.READ, black_hole, cue="Checking your list.", service="Todoist",
        ),
        Tool(
            "reschedule_task",
            "Give a Todoist task a new due date (natural language, e.g. 'Saturday 10am').",
            schema({"task_id": {"type": "string"}, "due": {"type": "string"}}, ["task_id", "due"]),
            Tier.CREATE_FOR_YOU, reschedule, service="Todoist",
            read_back=lambda a: f"Move that task to {a['due']}.",
        ),
        Tool(
            "add_reminder",
            "Create a to-do or reminder in Todoist. Give `due` in natural language with a time "
            "(e.g. 'Sunday 10am') when it should ping the phone at that moment.",
            schema({"content": {"type": "string"}, "due": {"type": "string"}}, ["content"]),
            Tier.CREATE_FOR_YOU, add_task, service="Todoist",
            read_back=lambda a: f"Add a reminder: {a['content']}" + (f", due {a['due']}." if a.get("due") else "."),
        ),
        Tool(
            "list_tasks",
            "List Todoist tasks. `filter` uses Todoist filter syntax, e.g. 'today | overdue', 'tomorrow', '7 days'.",
            schema({"filter": {"type": "string"}}),
            Tier.READ, list_tasks, cue="Checking your list.", service="Todoist",
        ),
        Tool(
            "complete_task",
            "Mark a Todoist task done by its id (find it with list_tasks first). Also how a task is "
            "dropped: deleting isn't supported, and a done task can be reopened.",
            schema({"task_id": {"type": "string"}}, ["task_id"]),
            Tier.CREATE_FOR_YOU, complete_task, service="Todoist",
            read_back=lambda a: "Mark that task done.",
        ),
        Tool(
            "add_to_shopping_list",
            "Add items to the running shopping list, under the store they're bought at.",
            schema({"items": {"type": "array", "items": {"type": "string"}}, "store": {"type": "string"}}, ["items"]),
            Tier.CREATE_FOR_YOU, add_to_shopping, service="Todoist",
            read_back=lambda a: f"Add {', '.join(a['items'])} to the {a.get('store') or config.default_store} list.",
        ),
        Tool(
            "read_shopping_list",
            "Read the shopping list, grouped by store, optionally for one store. Tick items off with "
            "complete_task using their ids.",
            schema({"store": {"type": "string"}}),
            Tier.READ, read_shopping, service="Todoist",
        ),
    ]

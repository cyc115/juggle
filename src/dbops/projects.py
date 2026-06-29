"""dbops.projects — Project CRUD and project-thread relationship mixin.

Owns: create/get/list/close/open/update projects, match-profile management,
project-correction log, project-thread counting/querying.
Must not own: thread internals, agent pool, notifications, session state.
"""

from __future__ import annotations

from dbops.schema import INBOX_PROJECT_ID, _now


class ProjectsMixin:
    """Mixin for project CRUD and project/thread relationship queries."""

    def _next_project_label(self, used: set) -> str:
        i = 1
        while True:
            label = f"P{i}"
            if label not in used:
                return label
            i += 1

    def create_project(
        self,
        name: str,
        objective: str,
        success_criteria: str = "[]",
        out_of_scope: str = "",
    ) -> str:
        with self._connect() as conn:
            used = {r[0] for r in conn.execute("SELECT id FROM projects").fetchall()}
            pid = self._next_project_label(used)
            now = _now()
            conn.execute(
                "INSERT INTO projects (id,name,objective,success_criteria,out_of_scope,status,created_at,last_active) "
                "VALUES (?,?,?,?,?,'active',?,?)",
                (pid, name, objective, success_criteria, out_of_scope, now, now),
            )
            conn.commit()
        return pid

    def get_project(self, project_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_projects(self, include_archived: bool = False) -> list[dict]:
        with self._connect() as conn:
            q = (
                "SELECT * FROM projects"
                if include_archived
                else "SELECT * FROM projects WHERE status NOT IN ('archived', 'closed')"
            )
            return [dict(r) for r in conn.execute(q).fetchall()]

    def get_active_projects(self) -> list[dict]:
        """Active projects excluding INBOX — used for LLM assignment prompts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status NOT IN ('archived', 'closed') "
                "AND id != 'INBOX' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_projects_with_state(self) -> list[dict]:
        """All projects including closed, with thread_count. Used for project list command."""
        with self._connect() as conn:
            projects = [
                dict(r)
                for r in conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
            ]
            result = []
            for p in projects:
                count = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE kind='conversation' "
                    "AND project_id=?",
                    (p["id"],),
                ).fetchone()[0]
                result.append({**p, "thread_count": count})
        return result

    def close_project(
        self, project_id: str, project_summary: str, thread_summaries: dict
    ) -> None:
        """Close a project: hide threads, write summaries, release busy agents. Guards INBOX."""
        if project_id == INBOX_PROJECT_ID:
            raise ValueError("Cannot close the INBOX project")
        now = _now()
        with self._connect() as conn:
            thread_rows = conn.execute(
                "SELECT id FROM nodes WHERE kind='conversation' AND project_id=?",
                (project_id,),
            ).fetchall()
            thread_ids = [r["id"] for r in thread_rows]

            conn.execute(
                "UPDATE projects SET status='closed', closed_at=?, summary=? WHERE id=?",
                (now, project_summary, project_id),
            )
            for tid in thread_ids:
                conn.execute(
                    "UPDATE threads SET show_in_list=0 WHERE id=?",
                    (tid,),
                )
                # P8 Task 4.2: get_thread reads nodes, so mirror the hide there too.
                conn.execute(
                    "UPDATE nodes SET show_in_list=0 WHERE id=? AND kind='conversation'",
                    (tid,),
                )
            if thread_ids:
                placeholders = ",".join("?" * len(thread_ids))
                conn.execute(
                    f"UPDATE agents SET status='idle', assigned_thread=NULL "
                    f"WHERE assigned_thread IN ({placeholders}) AND status='busy'",
                    thread_ids,
                )
            conn.commit()

    def open_project(self, project_id: str) -> None:
        """Restore a closed project: set active, clear closed_at, show all its threads."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET status='active', closed_at=NULL WHERE id=?",
                (project_id,),
            )
            conn.execute(
                "UPDATE threads SET show_in_list=1 WHERE project_id=?",
                (project_id,),
            )
            # P8 Task 4.2: get_thread reads nodes, so mirror the unhide there too.
            conn.execute(
                "UPDATE nodes SET show_in_list=1 WHERE project_id=? "
                "AND kind='conversation'",
                (project_id,),
            )
            conn.commit()

    def update_project(self, project_id: str, **kwargs) -> None:
        allowed = {
            "name",
            "objective",
            "success_criteria",
            "out_of_scope",
            "status",
            "last_active",
            "match_profile",
            "profile_synth_at",
            "profile_dirty",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE projects SET {set_clause} WHERE id = ?",
                (*fields.values(), project_id),
            )
            conn.commit()

    def set_match_profile(self, project_id: str, match_profile: str) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET match_profile=?, profile_synth_at=?, profile_dirty=0 WHERE id=?",
                (match_profile, now, project_id),
            )
            conn.commit()

    def mark_project_dirty(self, project_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET profile_dirty=1 WHERE id=?",
                (project_id,),
            )
            conn.commit()

    def get_dirty_projects(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE profile_dirty=1 AND status NOT IN ('archived','closed') AND id != 'INBOX'",
            ).fetchall()
        return [dict(r) for r in rows]

    def count_threads_by_project(self, project_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='conversation' "
                "AND project_id = ? AND show_in_list = 1",
                (project_id,),
            ).fetchone()
            return row[0] if row else 0

    def get_threads_by_project(self, project_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE kind = 'conversation' AND project_id = ? "
                "AND show_in_list = 1 ORDER BY last_active_at DESC",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def log_project_correction(
        self, topic: str, from_project: str, to_project: str
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_corrections (topic, from_project, to_project, created_at) VALUES (?, ?, ?, ?)",
                (topic, from_project, to_project, now),
            )
            conn.commit()

    def get_recent_corrections(self, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT topic, from_project, to_project, created_at FROM project_corrections ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_human_assigned_threads_by_project(
        self, project_id: str, limit: int = 3
    ) -> list[dict]:
        """Human-assigned conversations for a project (node vocab: title/
        last_active_at). P8 Task 4.2: reads kind='conversation' nodes."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, last_active_at FROM nodes "
                "WHERE kind='conversation' AND project_id = ? "
                "AND assigned_by = 'human' AND show_in_list = 1 "
                "ORDER BY last_active_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

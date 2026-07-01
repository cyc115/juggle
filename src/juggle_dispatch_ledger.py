"""juggle_dispatch_ledger — record the durable agent_runs ledger row for a
dispatch. Extracted from juggle_dispatch_core (2026-06-30 orchestration-metrics)
so the dispatch path stays within its LOC budget while the metrics stamps land.

Best-effort: a ledger failure NEVER breaks dispatch. Captures VCS provenance plus
the metrics stamps — prompt identity (fingerprint/version/bytes) and agent_cwd,
the agent's REAL worktree cwd used to resolve its Claude Code transcript.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger("juggle-dispatch")


def record_dispatch_run(
    db, *, thread_id, agent, agent_id, role, full_prompt, model, harness,
    prompt_version,
) -> None:
    """Insert the agent_runs row for a dispatch (best-effort — never raises)."""
    try:
        from dbops import db_graph, db_topics
        import juggle_prompt_metrics as _pm

        _thread = (db.get_thread(thread_id) or {}) if thread_id else {}
        _task = db_graph.get_task_by_thread(db, thread_id) if thread_id else None
        _topic = db_topics.get_topic_by_thread(db, thread_id) if thread_id else None
        _repo_path = agent.get("repo_path") or (_thread.get("worktree_path") if _thread else None)
        _vcs_type = _before_sha = _was_dirty = None
        if _repo_path:
            try:
                import vcs as _vcs
                _vcs_type = _vcs.detect(_repo_path)
                _backend = _vcs.get_backend(_vcs_type)
                if _backend:
                    _before_sha = _backend.head(_repo_path)
                    _was_dirty = _backend.is_dirty(_repo_path)
            except Exception:
                pass
        if thread_id:
            db.supersede_open_runs(thread_id)
        # agent_cwd = the agent's REAL worktree cwd (transcript key); the main repo
        # only for --allow-main runs where cwd == repo.
        _agent_cwd = (_thread.get("worktree_path") if _thread else None) or _repo_path
        _pv = prompt_version or os.environ.get("JUGGLE_PROMPT_VERSION") or "v0"
        run_id = db.insert_agent_run(
            thread_id=thread_id,
            input_prompt=full_prompt,
            agent_id=agent_id,
            role=role,
            model=model,
            harness=harness,
            project_id=_thread.get("project_id") if _thread else None,
            topic_id=_topic["id"] if _topic else None,
            task_id=_task["id"] if _task else None,
            repo_path=_repo_path,
            vcs_type=_vcs_type,
            before_sha=_before_sha,
            was_dirty=_was_dirty,
            prompt_fingerprint=_pm.prompt_fingerprint(full_prompt),
            prompt_version=_pv,
            prompt_bytes=_pm.prompt_bytes_of(full_prompt),
            agent_cwd=_agent_cwd,
        )
        db.update_agent(agent_id, current_run_id=run_id)
    except Exception as exc:
        _log.warning("ledger insert failed: %s", exc)

from __future__ import annotations

import os
from pathlib import Path

import agent_legacy as legacy
from flask import request

from preview_manager import register_preview_routes


APP_VERSION = "1.22.0"
legacy.APP_VERSION = APP_VERSION

# Preserve the existing public module surface for tests and integrations.
app = legacy.app
ROOT = legacy.ROOT
load_config = legacy.load_config
save_config = legacy.save_config
RunState = legacy.RunState
session_from_config = legacy.session_from_config
functional_worker = legacy.functional_worker
api_soak_worker = legacy.api_soak_worker
browser_worker = legacy.browser_worker
behavioral_worker = legacy.behavioral_worker
external_script_worker = legacy.external_script_worker

preview_manager = register_preview_routes(app, ROOT, APP_VERSION)


def __getattr__(name):
    return getattr(legacy, name)


@app.after_request
def add_ui_preview_navigation(response):
    """Add the new feature entry without coupling Preview Lab to legacy dashboard JS."""
    try:
        if request.path == "/" and response.is_sequence and "text/html" in (response.content_type or ""):
            html = response.get_data(as_text=True)
            marker = '<div class="top-actions">'
            if marker in html and 'href="/ui-preview"' not in html:
                link = (
                    '<a href="/ui-preview" style="display:inline-flex;align-items:center;'
                    'padding:8px 12px;border:1px solid #d8dfeb;border-radius:10px;'
                    'background:#fff;color:#25324a;text-decoration:none;font-weight:700">'
                    'UI Preview Lab</a>'
                )
                response.set_data(html.replace(marker, marker + link, 1))
                response.headers["Content-Length"] = str(len(response.get_data()))
    except Exception:
        legacy.app.logger.exception("Could not inject UI Preview Lab navigation")
    return response


if __name__ == "__main__":
    host = os.environ.get("MESFLOW_QA_HOST", "127.0.0.1")
    port = int(os.environ.get("MESFLOW_QA_PORT", "8095"))
    print(f"MESFlow QA Center V{APP_VERSION}: http://{host}:{port}", flush=True)
    legacy._resume_persistent_run_after_startup()
    app.run(host=host, port=port, debug=False, threaded=True)

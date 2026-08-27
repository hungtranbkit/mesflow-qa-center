from __future__ import annotations

from typing import Any

import requests

from .models import Step, StepResult


class ApiDriver:
    name = "API"

    def __init__(self, base_url: str, session: requests.Session | None = None, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def execute(self, step: Step, context: dict[str, Any]) -> StepResult:
        args = {k: (context.get(v[1:]) if isinstance(v, str) and v.startswith("$") else v)
                for k, v in step.arguments.items()}
        method = str(args.pop("method", "GET")).upper()
        path = str(args.pop("path", ""))
        try:
            response = self.session.request(method, self.base_url + path, timeout=self.timeout, **args)
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text[:2000]}
            actual = {"status": response.status_code, "body": body}
            expected_status = int(step.expected.get("status", 200))
            ok = response.status_code == expected_status
            save_as = step.expected.get("save_as")
            if ok and save_as:
                context[str(save_as)] = body
            return StepResult(ok, actual, error="" if ok else f"expected HTTP {expected_status}")
        except requests.RequestException as exc:
            return StepResult(False, {"exception": type(exc).__name__}, error=str(exc))


class KioskEmulatorDriver(ApiDriver):
    name = "KIOSK_EMULATOR"

    def __init__(self, base_url: str, device_uuid: str, **kwargs: Any):
        super().__init__(base_url, **kwargs)
        self.device_uuid = device_uuid
        self.online = True
        self.queue: list[Step] = []

    def execute(self, step: Step, context: dict[str, Any]) -> StepResult:
        if step.action == "network_offline":
            self.online = False
            return StepResult(True, {"online": False})
        if step.action == "network_reconnect":
            self.online = True
            queued = list(self.queue)
            self.queue.clear()
            results = [super(KioskEmulatorDriver, self).execute(item, context) for item in queued]
            return StepResult(all(x.ok for x in results), {"replayed": len(results), "results": [x.actual for x in results]})
        enriched = Step(step.action, {**step.arguments, "json": {**step.arguments.get("json", {}),
                                                                 "device_uuid": self.device_uuid}}, step.expected)
        if not self.online:
            self.queue.append(enriched)
            return StepResult(True, {"queued": True, "queue_size": len(self.queue)})
        return super().execute(enriched, context)

from dataclasses import dataclass


@dataclass(frozen=True)
class PersonaProfile:
    name: str
    primary_surface: str
    permissions: tuple[str, ...]
    behaviors: tuple[str, ...]


PERSONA_PROFILES = {
    "WORKER": PersonaProfile("WORKER", "/kiosk", ("production.execute",), ("scan", "start", "finish", "mistake", "retry")),
    "SUPERVISOR": PersonaProfile("SUPERVISOR", "/app", ("production.read", "session.correct", "exception.handle"), ("filter", "drill_down", "correct", "refresh")),
    "ADMIN": PersonaProfile("ADMIN", "/app", ("admin",), ("create", "clone", "import", "configure", "duplicate")),
    "VIEWER": PersonaProfile("VIEWER", "/app", ("production.read",), ("filter", "sort", "drill_down", "compare")),
}

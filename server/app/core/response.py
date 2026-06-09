from typing import Any


def ok(data: Any, message: str = "") -> dict[str, Any]:
    return {"code": 0, "data": data, "message": message}


def fail(message: str, code: int = 1, data: Any = None) -> dict[str, Any]:
    return {"code": code, "data": data, "message": message}

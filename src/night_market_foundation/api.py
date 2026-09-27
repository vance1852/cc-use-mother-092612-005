"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .plaques import PlaqueService
from .service import DomainService
from .storage import Database


def _plaque_service(service: DomainService) -> PlaqueService:
    return PlaqueService(service.database, service.clock)


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        plaques = _plaque_service(service)
        query = parse_qs(parsed.query)

        if method == "POST" and parsed.path == "/plaques":
            result = plaques.register_plaque(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/plaques/deploy":
            result = plaques.deploy_plaque(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/relocations":
            result = plaques.request_relocation(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/relocations/handover":
            return 200, plaques.handover_relocation(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/relocations/cancel":
            return 200, plaques.cancel_relocation(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/scans/upload":
            report = plaques.upload_scans(**body)
            return 202, {"accepted": report.accepted, "replayed": report.replayed,
                         "forked": report.forked,
                         "results": [item.__dict__ for item in report.results]}
        if method == "GET" and parsed.path == "/plaques/position":
            plaque_id = query.get("plaque_id", [""])[0]
            if not plaque_id:
                raise ValidationError("plaque_id 不能为空")
            return 200, plaques.plaque_position(plaque_id).__dict__
        if method == "GET" and parsed.path == "/deployments":
            deployment_id = query.get("deployment_id", [""])[0]
            if not deployment_id:
                raise ValidationError("deployment_id 不能为空")
            return 200, plaques.content_summary(deployment_id)
        if method == "GET" and parsed.path == "/inspection-queue":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            kind = query.get("kind", [None])[0]
            status = query.get("status", [None])[0]
            items = plaques.list_inspection_queue(site_id=site_id, kind=kind, status=status)
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "POST" and parsed.path == "/inspection-cases/claim":
            return 200, plaques.claim_case(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/inspection-cases/resolve":
            return 200, plaques.resolve_case(actor_id=actor_id, **body)
        if method == "GET" and parsed.path == "/inspection-cases/supplements":
            case_id = query.get("case_id", [""])[0]
            if not case_id:
                raise ValidationError("case_id 不能为空")
            return 200, {"items": plaques.list_supplements(case_id)}
        if method == "GET" and parsed.path == "/statistics/anonymous":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, plaques.anonymous_statistics(site_id=site_id,
                                                     day=query.get("day", [None])[0])
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

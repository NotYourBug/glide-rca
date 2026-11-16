"""Bounded, read-only checks against the local M0 observability backends."""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BYTES = 10 * 1024 * 1024
METRICS = ("request_rate", "error_rate", "latency_p95_ms", "ad_cpu", "ad_memory_bytes")


class ProbeError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Ignore proxy environment variables so local telemetry never leaves this host.
OPENER = build_opener(ProxyHandler({}), NoRedirect())


def validate_url(url: str, *, query_allowed: bool = False) -> str:
    if not isinstance(url, str) or any(ord(c) < 33 for c in url) or "\\" in url:
        raise ValueError("Endpoint must be a loopback HTTP URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or (parsed.query and not query_allowed)):
        raise ValueError("Endpoint must be loopback HTTP without credentials or fragments")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid endpoint port")
    return url.rstrip("/")


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    for key in ("environment_id", "service", "resource_label", "resource_value"):
        if not isinstance(config.get(key), str) or not re.fullmatch(r"[\w.-]{1,128}", config[key], re.ASCII):
            raise ValueError(f"Invalid {key}")
    for key in ("prometheus_url", "opensearch_url", "jaeger_url"):
        validate_url(config.get(key))
    queries = config.get("metric_queries")
    if not isinstance(queries, dict) or any(
        not isinstance(queries.get(key), str) or not 1 <= len(queries[key]) <= 4096 for key in METRICS
    ):
        raise ValueError("All five metric queries must be nonempty strings")
    if not isinstance(config.get("log_service_field"), str) or not re.fullmatch(
        r"[\w.]{1,128}", config["log_service_field"], re.ASCII
    ):
        raise ValueError("Invalid log_service_field")
    return config


def validate_window(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        first, last = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))
        if any(value.tzinfo is None or value.utcoffset().total_seconds() != 0 for value in (first, last)):
            raise ValueError("Use explicit UTC timestamps")
        if not 0 < (last - first).total_seconds() <= 900:
            raise ValueError("Window must be positive and at most 15 minutes")
        return first, last
    except (AttributeError, TypeError, OverflowError) as error:
        raise ValueError("Invalid UTC window") from error


def request_json(url: str, body: dict | None = None) -> tuple[int, object]:
    validate_url(url, query_allowed=True)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    encoded = None
    if body is not None:
        encoded = json.dumps(body, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=encoded, headers=headers)
    try:
        try:
            response = OPENER.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ProbeError("response_too_large", "Response exceeded 10 MiB", status)
        try:
            def reject_constant(value):
                raise ValueError("Nonfinite JSON constant")
            return status, json.loads(content.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeError, ValueError) as error:
            kind = "http_error" if status >= 300 else "invalid_json"
            raise ProbeError(kind, "Response is not valid JSON", status) from error
    except (URLError, OSError, TimeoutError) as error:
        raise ProbeError("transport_error", type(error).__name__) from error


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def extract_spans(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("Trace response must be an object")
    result = payload.get("result", payload)
    resources = result.get("resourceSpans") if isinstance(result, dict) else None
    if not isinstance(resources, list):
        raise ValueError("Missing resourceSpans")
    spans = []
    for resource in resources:
        attributes = resource.get("resource", {}).get("attributes", [])
        service = next((a.get("value", {}).get("stringValue") for a in attributes
                        if a.get("key") == "service.name"), None)
        for scope in resource.get("scopeSpans", []):
            if scope.get("spans") and (not isinstance(service, str) or not service.strip()):
                raise ValueError("Spans missing service.name")
            for span in scope.get("spans", []):
                if not all(span.get(key) for key in ("traceId", "spanId", "startTimeUnixNano", "endTimeUnixNano")):
                    raise ValueError("Incomplete span identity/timestamps")
                if int(span["endTimeUnixNano"]) < int(span["startTimeUnixNano"]):
                    raise ValueError("Span ends before it starts")
                spans.append({**span, "service_name": service})
    return spans


def run_probe(config: dict, start: str, end: str, output: Path) -> dict:
    validate_config(config)
    first, last = validate_window(start, end)
    output.mkdir(parents=True, exist_ok=False)
    samples = output / "samples"
    samples.mkdir()
    report = {
        "schema_version": "m0-probe.v1", "environment_id": config["environment_id"],
        "window": {"start": start, "end": end},
        "captured_at": datetime.now(timezone.utc).isoformat(), "checks": [],
        "limits": {"response_bytes": MAX_BYTES, "logs": 200, "trace_search_depth": 100,
                   "trace_details": 5, "query_step_seconds": 10},
        "scope": "bounded interface samples; not a complete diagnostic snapshot",
    }

    def check(name, url, inspect, body=None):
        item = {"name": name, "url": url, "status": "fail"}
        try:
            status, payload = request_json(url, body)
            item["http_status"] = status
            write_json(samples / f"{name}.json", {"request_body": body, "response": payload})
            item["sample"] = f"samples/{name}.json"
            if status == 404 and name == "trace_search" and isinstance(payload, dict) and payload.get("message") == "No traces found":
                item.update(status="needs_review", reason="empty")
                return None
            if status != 200:
                item["reason"] = "http_error"
                return None
            details = inspect(payload)
            item.update(details)
            item.setdefault("reason", "ok")
            item["status"] = details.get("status", "pass")
            return payload
        except ProbeError as error:
            item.update(reason=error.kind, http_status=error.status, detail=str(error))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
            item.update(reason="invalid_shape", detail=str(error))
        finally:
            report["checks"].append(item)
            write_json(output / "probe-report.json", report)
        return None

    for name in METRICS:
        def inspect_metric(payload, metric_name=name):
            if payload.get("status") != "success":
                raise ValueError("Prometheus query did not succeed")
            series = payload["data"]["result"]
            if not isinstance(series, list):
                raise ValueError("Missing metric result list")
            if metric_name.startswith("ad_"):
                series = [s for s in series if s.get("metric", {}).get(config["resource_label"]) == config["resource_value"]]
                if not series:
                    return {"status": "needs_review", "reason": "resource_identity_missing"}
            count = sum(1 for s in series for point in s.get("values", [])
                        if len(point) == 2 and math.isfinite(float(point[1])))
            if not count:
                return {"status": "needs_review", "reason": "empty_or_nonfinite", "series": len(series)}
            return {"series": len(series), "finite_points": count,
                    "labels": [s.get("metric", {}) for s in series[:10]]}
        query = urlencode({"query": config["metric_queries"][name], "start": first.timestamp(),
                           "end": last.timestamp(), "step": 10})
        check(name, config["prometheus_url"].rstrip("/") + "/api/v1/query_range?" + query, inspect_metric)

    def inspect_mapping(payload):
        if not isinstance(payload, dict) or not payload or not all(
            isinstance(value, dict) and "mappings" in value for value in payload.values()
        ):
            raise ValueError("No log index mappings")
        return {"indices": sorted(payload)}

    logs_url = config["opensearch_url"].rstrip("/") + "/otel-logs-*"
    check("log_mapping", logs_url + "/_mapping", inspect_mapping)

    def inspect_logs(payload):
        flags = {"timed_out": payload.get("timed_out", False),
                 "failed_shards": payload.get("_shards", {}).get("failed", 0)}
        partial = flags["timed_out"] or flags["failed_shards"] > 0
        hits = payload["hits"]["hits"]
        if not isinstance(hits, list):
            raise ValueError("Missing log hits")
        if not hits:
            return {**flags, "status": "needs_review", "records": 0,
                    "reason": "partial_results" if partial else "empty"}
        sources = [hit["_source"] for hit in hits]
        if any("@timestamp" not in source or "body" not in source for source in sources):
            raise ValueError("Logs missing event time/body")
        if any(source.get("resource", {}).get("service.name", source.get("resource", {}).get("service", {}).get("name")) != config["service"] for source in sources):
            raise ValueError("Log service identity does not match")
        total = payload["hits"].get("total", len(hits))
        total_value = total.get("value", len(hits)) if isinstance(total, dict) else total
        lower_bound = isinstance(total, dict) and total.get("relation") == "gte"
        return {**flags, "records": len(hits), "total": total,
                "truncated": lower_bound or total_value > len(hits),
                "status": "needs_review" if partial else "pass",
                "reason": "partial_results" if partial else "ok",
                "trace_id_records": sum(bool(source.get("traceId")) for source in sources),
                "fields": sorted(sources[0])}

    body = {"size": 200, "track_total_hits": True,
            "sort": [{"@timestamp": "asc"}],
            "query": {"bool": {"filter": [
                {"range": {"@timestamp": {"gte": start, "lte": end}}},
                {"term": {config["log_service_field"]: config["service"]}},
            ]}}}
    check("logs", logs_url + "/_search", inspect_logs, body)

    def inspect_services(payload):
        services = payload.get("services")
        if services is None:
            services = payload.get("result", {}).get("services")
        if not isinstance(services, list) or config["service"] not in services:
            raise ValueError("Target service not listed")
        return {"services": services}

    jaeger = config["jaeger_url"].rstrip("/")
    check("trace_services", jaeger + "/services", inspect_services)

    def inspect_search(payload):
        spans = extract_spans(payload)
        if not spans:
            return {"status": "needs_review", "reason": "empty", "spans": 0}
        ids = sorted({span["traceId"] for span in spans})
        return {"spans": len(spans), "trace_ids": ids, "truncated": len(ids) >= 100,
                "detail_selection": "first five trace IDs in lexical order"}

    query = urlencode({"query.serviceName": config["service"], "query.startTimeMin": start,
                       "query.startTimeMax": end, "query.searchDepth": 100})
    found = check("trace_search", jaeger + "/traces?" + query, inspect_search)
    if found is not None:
        try:
            ids = sorted({span["traceId"] for span in extract_spans(found)})[:5]
            for index, trace_id in enumerate(ids, 1):
                if not re.fullmatch(r"[0-9a-fA-F]{1,64}", trace_id):
                    raise ValueError("Trace ID is not hexadecimal")

                def inspect_trace(payload, expected=trace_id):
                    spans = extract_spans(payload)
                    if not spans or any(span["traceId"] != expected for span in spans):
                        raise ValueError("Trace detail identity mismatch")
                    keys = {(s["traceId"], s["spanId"]): s for s in spans}
                    edges = set()
                    orphan_count = 0
                    for span in spans:
                        parent = keys.get((span["traceId"], span.get("parentSpanId")))
                        if span.get("parentSpanId") and parent is None:
                            orphan_count += 1
                        if parent and parent["service_name"] != span["service_name"]:
                            edges.add((parent["service_name"], span["service_name"]))
                    return {"trace_id": expected, "spans": len(spans), "orphan_spans": orphan_count,
                            "cross_service_edges": sorted([list(edge) for edge in edges]),
                            "services": sorted({s["service_name"] for s in spans if s["service_name"]})}
                check(f"trace-{index}", jaeger + "/traces/" + trace_id, inspect_trace)
        except (ValueError, TypeError, KeyError) as error:
            report["checks"].append({"name": "trace_details", "status": "fail", "reason": "invalid_shape", "detail": str(error)})
    write_json(output / "probe-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.config.stat().st_size > 512 * 1024:
            raise ValueError("Configuration exceeds 512 KiB")
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        validate_config(config)
        validate_window(args.start, args.end)
        report = run_probe(config, args.start, args.end, args.output)
    except (OSError, ValueError) as error:
        print(f"Probe configuration/output error: {error}", file=sys.stderr)
        return 2
    passed = sum(check["status"] == "pass" for check in report["checks"])
    print(f"{passed}/{len(report['checks'])} checks passed; report: {args.output / 'probe-report.json'}")
    return 0 if passed == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

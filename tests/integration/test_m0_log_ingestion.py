"""Run explicitly: python -m unittest discover -s tests/integration -v."""

import json
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.m0_probe import request_json, write_json


def otlp_value(value):
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [otlp_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [
            {"key": key, "value": otlp_value(item)} for key, item in value.items()
        ]}}
    raise TypeError(f"Unsupported fixture type: {type(value).__name__}")


def timestamp(nanoseconds):
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    return (datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            + ("." + f"{fraction:09d}".rstrip("0") if fraction else "") + "Z")


class LogIngestionTests(unittest.TestCase):
    def test_heterogeneous_logs_preserve_source_and_core_queries(self):
        trace_id = uuid.uuid4().hex
        output = (Path(__file__).resolve().parents[2] / "artifacts" / "m0-log-fix"
                  / f"controlled-{trace_id}")
        output.mkdir(parents=True, exist_ok=False)
        print(f"Controlled log evidence: {output}")
        service = "glide-log-validation"
        cases = [
            ("http-scalar", {"http": "8080"}, {"prefix": "scalar"}, "scalar"),
            ("http-dotted", {"http.request.method": "GET"}, {"prefix.leaf": "dotted"}, "dotted"),
            ("http-nested", {"http": {"request": {"method": "POST"}}},
             {"prefix": {"leaf": "nested"}}, "nested"),
            ("http-combined", {"http": "8080", "http.request.method": "PATCH"},
             {"prefix": "scalar", "prefix.leaf": "dotted"}, "combined"),
            ("number-first", {"vary.forward": 42}, {"vary.forward": 42}, "number first"),
            ("string-second", {"vary.forward": "forty-two"},
             {"vary.forward": "forty-two"}, "string second"),
            ("string-first", {"vary.reverse": "forty-two"},
             {"vary.reverse": "forty-two"}, "string first"),
            ("number-second", {"vary.reverse": 42}, {"vary.reverse": 42}, "number second"),
            ("array-bool-null", {"array": [1, "two", True, None, {"ok": False}],
                                 "bool": False, "null": None},
             {"array": [False, None, {"nested": "value"}]}, "mixed values"),
            ("long-error", {"error.stacktrace": "E" * 40_000},
             {"long.error": "E" * 40_000}, "long error"),
            ("structured-body", {"body.kind": "structured"}, {"prefix": "structured"},
             {"message": "structured", "number": 5, "nested": {"ok": True, "null": None},
              "items": [1, "two"]}),
        ]
        base_time = time.time_ns() // 1_000_000_000 * 1_000_000_000 + 123_456_789
        scope_logs = []
        expected = {}
        for index, (name, attributes, scope_attributes, body) in enumerate(cases, 1):
            span_id = f"{index:016x}"
            event_time = base_time + index
            scope_logs.append({
                "scope": {"name": "glide-m0-log-regression", "version": "1",
                          "attributes": otlp_value(scope_attributes)["kvlistValue"]["values"]},
                "logRecords": [{
                    "timeUnixNano": str(event_time),
                    "observedTimeUnixNano": str(event_time - 1_000_000_000),
                    "severityNumber": 9, "severityText": "INFO",
                    "body": otlp_value(body),
                    "attributes": otlp_value(attributes)["kvlistValue"]["values"],
                    "traceId": trace_id, "spanId": span_id,
                }],
            })
            expected[span_id] = (name, attributes, scope_attributes, body, event_time)
        payload = {"resourceLogs": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service}},
                {"key": "service.namespace", "value": {"stringValue": "glide-m0-validation"}},
            ]},
            "scopeLogs": scope_logs,
        }]}
        write_json(output / "input.json", payload)
        status, response = request_json("http://127.0.0.1:8080/otlp-http/v1/logs", payload)
        write_json(output / "otlp-response.json", {"status": status, "response": response})
        self.assertEqual(status, 200)
        self.assertIsInstance(response, dict)
        self.assertEqual(int(response.get("partialSuccess", {}).get("rejectedLogRecords", 0)), 0)

        query = {"size": len(cases) + 1, "track_total_hits": True,
                 "query": {"bool": {"filter": [
                     {"term": {"traceId": trace_id}},
                     {"term": {"resource.service.name.keyword": service}},
                     {"range": {"@timestamp": {"gte": timestamp(base_time),
                                              "lte": timestamp(base_time + len(cases) + 1)}}},
                 ]}}}
        write_json(output / "search-request.json", query)
        deadline = time.monotonic() + 60
        attempt = 0
        while True:
            attempt += 1
            status, response = request_json("http://127.0.0.1:9200/otel-logs-v2-*/_search", query)
            write_json(output / f"search-{attempt:03d}.json", {"status": status, "response": response})
            self.assertEqual(status, 200)
            self.assertFalse(response["timed_out"])
            self.assertEqual(response["_shards"]["failed"], 0)
            self.assertEqual(response["hits"]["total"]["relation"], "eq")
            count = response["hits"]["total"]["value"]
            self.assertLessEqual(count, len(cases), "Duplicate controlled records")
            if count == len(cases):
                break
            if time.monotonic() >= deadline:
                self.fail(f"Only {count}/{len(cases)} controlled logs indexed; evidence: {output}")
            time.sleep(2)

        hits = response["hits"]["hits"]
        self.assertEqual(len(hits), len(cases))
        by_span = {hit["_source"]["spanId"]: hit["_source"] for hit in hits}
        self.assertEqual(set(by_span), set(expected))
        for span_id, (name, attributes, scope_attributes, body, event_time) in expected.items():
            source = by_span[span_id]
            self.assertEqual(source["resource"]["service.name"], service, name)
            self.assertEqual(source["traceId"], trace_id, name)
            self.assertEqual(source["spanId"], span_id, name)
            self.assertEqual(source["@timestamp"], timestamp(event_time), name)
            self.assertIsInstance(source["observedTimestamp"], str, name)
            # SS4O 0.159.0 sets observedTimestamp at export, not from the input.
            scope = source["instrumentationScope"]
            self.assertEqual(scope["name"], "glide-m0-log-regression", name)
            self.assertEqual(scope["version"], "1", name)
            for stored, wanted in [(source["attributes"], attributes),
                                   (scope["attributes"], scope_attributes)]:
                subset = {key: stored[key] for key in wanted}
                self.assertEqual(json.dumps(subset, sort_keys=True),
                                 json.dumps(wanted, sort_keys=True), name)
            actual_body = json.loads(source["body"]) if isinstance(body, dict) else source["body"]
            self.assertEqual(actual_body, body, name)

        first_time = timestamp(base_time + 1)
        query["query"]["bool"]["filter"][2] = {
            "range": {"@timestamp": {"gte": first_time, "lte": first_time}}}
        for check in ("nanosecond", "body"):
            if check == "body":
                query["query"]["bool"]["filter"].append({"match": {"body": "scalar"}})
            status, response = request_json("http://127.0.0.1:9200/otel-logs-v2-*/_search", query)
            write_json(output / f"{check}-query.json",
                       {"request": query, "status": status, "response": response})
            self.assertEqual(status, 200, check)
            self.assertFalse(response["timed_out"], check)
            self.assertEqual(response["_shards"]["failed"], 0, check)
            self.assertEqual(response["hits"]["total"], {"value": 1, "relation": "eq"}, check)
            self.assertEqual(len(response["hits"]["hits"]), 1, check)
            self.assertEqual(response["hits"]["hits"][0]["_source"]["spanId"], f"{1:016x}", check)
        write_json(output / "result.json", {"trace_id": trace_id, "service": service,
                                           "records": len(cases), "status": "pass",
                                           "core_queries": ["nanosecond", "body"]})


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import m0_probe as probe


START = "2026-11-16T00:00:00Z"
END = "2026-11-16T00:05:00Z"


def config():
    return {
        "environment_id": "demo-local",
        "prometheus_url": "http://127.0.0.1:9090",
        "opensearch_url": "http://127.0.0.1:9200",
        "jaeger_url": "http://127.0.0.1:8080/jaeger/ui/api/v3",
        "service": "payment",
        "metric_queries": {name: "up" for name in probe.METRICS},
        "resource_label": "container_name",
        "resource_value": "ad",
        "log_service_field": "resource.service.name.keyword",
    }


class ValidationTests(unittest.TestCase):
    def test_valid_config_and_utc_window(self):
        self.assertEqual(probe.validate_config(config())["service"], "payment")
        start, end = probe.validate_window(START, END)
        self.assertEqual((end - start).total_seconds(), 300)

    def test_reject_nonlocal_credentials_redirect_query_and_bad_port(self):
        for url in ["http://example.com", "http://127.0.0.1.example.com",
                    "http://name:secret@localhost", "https://localhost",
                    "http://localhost/?token=secret", "http://localhost/#x",
                    "http://localhost:99999", "http://127.1", "http://127.0.0.1\\@example.com"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                probe.validate_url(url)

    def test_reject_invalid_or_unbounded_window(self):
        for start, end in [(END, START), (START, START),
                           ("2026-11-16T00:00:00", END),
                           (START, "2026-11-16T00:16:00Z"), ("bad", END)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                probe.validate_window(start, end)

    def test_reject_missing_query_and_invalid_service(self):
        bad = config()
        bad["metric_queries"].pop("ad_cpu")
        with self.assertRaises(ValueError):
            probe.validate_config(bad)
        bad = config()
        bad["service"] = "../payment"
        with self.assertRaises(ValueError):
            probe.validate_config(bad)

    def test_reject_unsafe_log_index_pattern(self):
        for pattern in ["*", "../other", "otel-logs-x,_all", "otel-logs-v2-*/_delete",
                        "otel-logs-x?", "otel-logs-foo bar", None]:
            bad = {**config(), "log_index_pattern": pattern}
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                probe.validate_config(bad)


class TransportTests(unittest.TestCase):
    def test_redirect_is_refused(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com"))

    def test_read_limit_and_invalid_json(self):
        class Response:
            status = 200
            def __init__(self, value):
                self.value = value
            def read(self, limit):
                return self.value[:limit]
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        for value in [b"x" * (probe.MAX_BYTES + 1), b"not json", b'{"n": NaN}']:
            with self.subTest(size=len(value)):
                with patch.object(probe.OPENER, "open", return_value=Response(value)):
                    with self.assertRaises(probe.ProbeError):
                        probe.request_json("http://localhost:8080/data")

    def test_timeout_retains_error_category(self):
        with patch.object(probe.OPENER, "open", side_effect=TimeoutError("timeout")):
            with self.assertRaises(probe.ProbeError) as result:
                probe.request_json("http://localhost:8080/data")
        self.assertEqual(result.exception.kind, "transport_error")


class ProbeTests(unittest.TestCase):
    def response(self, url, body=None):
        if "query_range" in url:
            return 200, {"status": "success", "data": {"result": [
                {"metric": {"container_name": "ad"}, "values": [[1794787200, "0"]]}]}}
        if "_mapping" in url:
            return 200, {"otel-logs-test": {"mappings": {"properties": {}}}}
        if "_search" in url:
            return 200, {"hits": {"total": {"value": 1}, "hits": [{"_source": {
                "@timestamp": START, "resource": {"service": {"name": "payment"}},
                "body": "sample", "severity": {"text": "WARN"}}}]}}
        if url.endswith("/services"):
            return 200, {"services": ["payment"]}
        spans = [{"traceId": "0011", "spanId": "aa", "parentSpanId": "",
                  "startTimeUnixNano": "1794787200123456789", "endTimeUnixNano": "1794787201123456789"}]
        return 200, {"result": {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "payment"}}]},
            "scopeSpans": [{"spans": spans}]}]}}

    def test_success_writes_report_samples_and_preserves_nanoseconds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            with patch.object(probe, "request_json", side_effect=self.response):
                report = probe.run_probe(config(), START, END, output)
            self.assertTrue(all(c["status"] == "pass" for c in report["checks"]), report)
            saved = json.loads((output / "probe-report.json").read_text())
            self.assertEqual(saved["schema_version"], "m0-probe.v1")
            sample = (output / "samples/trace-1.json").read_text()
            self.assertIn("1794787200123456789", sample)

    def test_configured_log_index_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "request_json", side_effect=self.response) as request:
                report = probe.run_probe({**config(), "log_index_pattern": "otel-logs-v2-*"},
                                         START, END, Path(directory) / "new")
            urls = [call.args[0] for call in request.call_args_list]
            self.assertIn("http://127.0.0.1:9200/otel-logs-v2-*/_mapping", urls)
            self.assertIn("http://127.0.0.1:9200/otel-logs-v2-*/_search", urls)
            self.assertEqual(report["log_index_pattern"], "otel-logs-v2-*")

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "keep.txt"
            sentinel.write_text("keep")
            with self.assertRaises(FileExistsError):
                probe.run_probe(config(), START, END, Path(directory))
            self.assertEqual(sentinel.read_text(), "keep")

    def test_backend_failure_leaves_report_and_does_not_stop_other_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "request_json", side_effect=probe.ProbeError("transport_error", "unavailable")):
                report = probe.run_probe(config(), START, END, Path(directory) / "new")
            self.assertGreaterEqual(len(report["checks"]), 8)
            self.assertTrue(all(c["status"] != "pass" for c in report["checks"]))

    def test_no_traces_and_bad_route_are_distinct(self):
        for payload, expected in [({"message": "No traces found"}, "empty"),
                                  ({"message": "Not Found"}, "http_error")]:
            def response(url, body=None):
                if "/traces?" in url:
                    return 404, payload
                return self.response(url, body)
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                with patch.object(probe, "request_json", side_effect=response):
                    report = probe.run_probe(config(), START, END, Path(directory) / "new")
                check = next(c for c in report["checks"] if c["name"] == "trace_search")
                self.assertEqual(check["reason"], expected)

    def test_empty_nan_and_wrong_container_metrics_do_not_pass(self):
        for series in [[], [{"metric": {}, "values": [[1, "NaN"]]}],
                       [{"metric": {"container_name": "ad"}, "values": [[1, "NaN"]]},
                        {"metric": {"container_name": "other"}, "values": [[1, "1"]]}],
                       [{"metric": {"container_name": "other"}, "values": [[1, "1"]]}]]:
            def response(url, body=None):
                if "query_range" in url:
                    return 200, {"status": "success", "data": {"result": series}}
                return self.response(url, body)
            with self.subTest(series=series), tempfile.TemporaryDirectory() as directory:
                with patch.object(probe, "request_json", side_effect=response):
                    report = probe.run_probe(config(), START, END, Path(directory) / "new")
                self.assertNotEqual(next(c for c in report["checks"] if c["name"] == "ad_cpu")["status"], "pass")

    def test_trace_without_service_identity_does_not_pass(self):
        def response(url, body=None):
            status, payload = self.response(url, body)
            if "/traces" in url:
                payload["result"]["resourceSpans"][0]["resource"] = {}
            return status, payload
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "request_json", side_effect=response):
                report = probe.run_probe(config(), START, END, Path(directory) / "new")
            self.assertEqual(next(c for c in report["checks"] if c["name"] == "trace_search")["status"], "fail")

    def test_ss4o_flat_resource_service_identity(self):
        def response(url, body=None):
            status, payload = self.response(url, body)
            if "_search" in url:
                payload['hits']['hits'][0]['_source']['resource'] = {'service.name': 'payment'}
            return status, payload
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "request_json", side_effect=response):
                report = probe.run_probe(config(), START, END, Path(directory) / 'new')
            self.assertEqual(next(c for c in report['checks'] if c['name'] == 'logs')['status'], 'pass')

    def test_partial_log_search_does_not_pass(self):
        for failure in [{"timed_out": True}, {"_shards": {"failed": 1}}]:
            def response(url, body=None):
                status, payload = self.response(url, body)
                if "_search" in url:
                    payload.update(failure)
                return status, payload
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                with patch.object(probe, "request_json", side_effect=response):
                    report = probe.run_probe(config(), START, END, Path(directory) / "new")
                result = next(c for c in report['checks'] if c['name'] == 'logs')
                self.assertEqual(result['status'], 'needs_review')
                self.assertEqual(result['reason'], 'partial_results')

    def test_lower_bound_log_total_is_marked_truncated(self):
        def response(url, body=None):
            status, payload = self.response(url, body)
            if "_search" in url:
                payload['hits']['total'] = {'value': 1, 'relation': 'gte'}
            return status, payload
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "request_json", side_effect=response):
                report = probe.run_probe(config(), START, END, Path(directory) / "new")
            self.assertTrue(next(c for c in report['checks'] if c['name'] == 'logs')['truncated'])


if __name__ == "__main__":
    unittest.main()

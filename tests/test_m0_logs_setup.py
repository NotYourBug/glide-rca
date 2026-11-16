import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import m0_logs_setup as setup
from scripts.m0_probe import MAX_BYTES, ProbeError


NOW = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
BASE = "http://127.0.0.1:9200"
INDEX = "otel-logs-v2-2026-11-16"


def template():
    return {
        "index_patterns": ["otel-logs-v2-*"], "priority": 200, "version": 2,
        "template": {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "_meta": {"schema": "glide-m0-logs.v2"}, "dynamic": "false",
                "properties": {"attributes": {"type": "object", "enabled": False}},
            },
        },
    }


def missing(kind):
    return 404, {"error": {"type": kind}, "status": 404}


def server_template(value):
    return 200, {"index_templates": [{"name": "glide-m0-logs-v2", "index_template": value}]}


def index_settings(extra=None):
    value = {"number_of_shards": "1", "number_of_replicas": "0",
             "provided_name": INDEX, "creation_date": "1794838070741", "uuid": "example",
             "version": {"created": "137287827"}, "replication": {"type": "DOCUMENT"}}
    value.update(extra or {})
    return 200, {INDEX: {"settings": {"index": value}}}


def aliases(value=None):
    return 200, {INDEX: {"aliases": value or {}}}


class SetupTests(unittest.TestCase):
    def test_missing_resources_created_once_and_index_read_back(self):
        saved = {}

        def get(url):
            if "/_index_template/" in url:
                return missing("resource_not_found_exception")
            if "index" in saved:
                if "_settings" in url:
                    return index_settings()
                if "_alias" in url:
                    return aliases()
                return 200, {INDEX: {"mappings": saved["index"]["mappings"]}}
            return missing("index_not_found_exception")

        def put(url, body):
            saved["template" if "/_index_template/" in url else "index"] = copy.deepcopy(body)
            return {"acknowledged": True}

        with patch.object(setup, "request_json", side_effect=get), patch.object(
            setup, "put_json", side_effect=put
        ) as writes:
            result = setup.setup_logs(BASE, template(), NOW)
        self.assertEqual(result["index"], INDEX)
        self.assertTrue(result["created_template"] and result["created_index"])
        self.assertEqual(writes.call_count, 2)
        self.assertEqual(writes.call_args_list[0].args[0], BASE + "/_index_template/glide-m0-logs-v2?create=true")
        self.assertEqual(writes.call_args_list[1].args[0], BASE + "/" + INDEX)
        self.assertEqual(saved["index"], template()["template"])

    def test_matching_resources_with_server_defaults_do_not_write(self):
        value = template()
        value["composed_of"] = []
        value["template"]["settings"] = {"index": {"number_of_shards": "1", "number_of_replicas": "0"}}
        mapping = copy.deepcopy(template()["template"]["mappings"])
        mapping["properties"]["attributes"].pop("type")
        with patch.object(setup, "request_json", side_effect=[
            server_template(value), (200, {INDEX: {"mappings": mapping}}), index_settings(), aliases()
        ]), patch.object(setup, "put_json") as writes:
            result = setup.setup_logs(BASE, template(), NOW)
        self.assertFalse(result["created_template"] or result["created_index"])
        writes.assert_not_called()

    def test_incompatible_template_never_written(self):
        value = template()
        value["template"]["mappings"]["properties"]["attributes"]["enabled"] = True
        with patch.object(setup, "request_json", return_value=server_template(value)), patch.object(
            setup, "put_json"
        ) as writes, self.assertRaises(ValueError):
            setup.setup_logs(BASE, template(), NOW)
        writes.assert_not_called()

    def test_incompatible_index_schema_properties_and_boolean_types_rejected(self):
        for defect in ("schema", "extra", "boolean"):
            mapping = copy.deepcopy(template()["template"]["mappings"])
            if defect == "schema":
                mapping["_meta"]["schema"] = "other"
            elif defect == "extra":
                mapping["properties"]["surprise"] = {"type": "text"}
            else:
                mapping["properties"]["attributes"]["enabled"] = 0
            with self.subTest(defect=defect), patch.object(setup, "request_json", side_effect=[
                missing("resource_not_found_exception"), (200, {INDEX: {"mappings": mapping}})
            ]), patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()

    def test_source_default_is_enabled_but_disabled_source_rejected(self):
        expected = template()
        expected["template"]["mappings"]["_source"] = {"enabled": True}
        for enabled in (None, False):
            mapping = copy.deepcopy(template()["template"]["mappings"])
            if enabled is not None:
                mapping["_source"] = {"enabled": enabled}
            with self.subTest(enabled=enabled), patch.object(setup, "request_json", side_effect=[
                server_template(expected), (200, {INDEX: {"mappings": mapping}}), index_settings(), aliases()
            ]), patch.object(setup, "put_json") as writes:
                if enabled is False:
                    with self.assertRaises(ValueError):
                        setup.setup_logs(BASE, expected, NOW)
                else:
                    setup.setup_logs(BASE, expected, NOW)
            writes.assert_not_called()

    def test_template_extra_write_behavior_is_rejected(self):
        for defect in ("pipeline", "component", "alias", "unknown"):
            value = template()
            if defect == "pipeline":
                value["template"]["settings"]["index"] = {"final_pipeline": "drop-all"}
            elif defect == "component":
                value["composed_of"] = ["drop-all"]
            elif defect == "alias":
                value["template"]["aliases"] = {"view": {"filter": {"match_none": {}}}}
            else:
                value["data_stream"] = {}
            with self.subTest(defect=defect), patch.object(setup, "request_json", side_effect=[
                server_template(value), (200, {INDEX: {"mappings": template()["template"]["mappings"]}}),
                index_settings(), aliases()
            ]), patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()

    def test_index_pipeline_block_filters_or_aliases_are_rejected_before_writes(self):
        for extra in [{"default_pipeline": "drop-all"}, {"final_pipeline": "drop-all"},
                      {"blocks": {"write": "true"}}, {"routing": {"allocation": {"include": {"zone": "other"}}}},
                      {"number_of_shards": "2"}, {"number_of_replicas": "1"}]:
            with self.subTest(extra=extra), patch.object(setup, "request_json", side_effect=[
                missing("resource_not_found_exception"),
                (200, {INDEX: {"mappings": template()["template"]["mappings"]}}),
                index_settings(extra), aliases()
            ]), patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()
        with patch.object(setup, "request_json", side_effect=[
            missing("resource_not_found_exception"),
            (200, {INDEX: {"mappings": template()["template"]["mappings"]}}),
            index_settings(), aliases({"filtered": {"filter": {"match_none": {}}}})
        ]), patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
            setup.setup_logs(BASE, template(), NOW)
        writes.assert_not_called()

    def test_index_explicit_no_pipeline_and_false_block_are_compatible(self):
        with patch.object(setup, "request_json", side_effect=[
            server_template(template()), (200, {INDEX: {"mappings": template()["template"]["mappings"]}}),
            index_settings({"default_pipeline": "_none", "final_pipeline": "_none", "blocks": {"write": "false"}}),
            aliases()
        ]), patch.object(setup, "put_json") as writes:
            setup.setup_logs(BASE, template(), NOW)
        writes.assert_not_called()

    def test_wrong_server_shapes_fail_closed(self):
        for payload in [[], {"index_templates": None}, {"index_templates": []},
                        {"index_templates": [{"name": "other", "index_template": template()}]}]:
            with self.subTest(payload=payload), patch.object(setup, "request_json", return_value=(200, payload)), \
                    patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()

    def test_ambiguous_or_wrongly_typed_server_settings_rejected(self):
        for settings in [{"index": {"number_of_shards": "9"}, "number_of_shards": 1,
                          "number_of_replicas": 0},
                         {"number_of_shards": True, "number_of_replicas": 0}]:
            value = template()
            value["template"]["settings"] = settings
            with self.subTest(settings=settings), patch.object(
                setup, "request_json", side_effect=[server_template(value),
                                                    (200, {INDEX: {"mappings": template()["template"]["mappings"]}})]
            ), patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()

    def test_invalid_endpoint_or_template_rejected_before_http(self):
        for url in ["http://example.com:9200", "http://name:secret@localhost:9200", "http://localhost:9200/?x=1"]:
            with self.subTest(url=url), patch.object(setup, "request_json") as read, self.assertRaises(ValueError):
                setup.setup_logs(url, template(), NOW)
            read.assert_not_called()
        value = template()
        value["index_patterns"] = ["*"]
        with patch.object(setup, "request_json") as read, self.assertRaises(ValueError):
            setup.setup_logs(BASE, value, NOW)
        read.assert_not_called()

    def test_get_http_error_and_unrelated_404_do_not_write(self):
        for response in [(403, {"error": "forbidden"}), (404, {"message": "Not found"})]:
            with self.subTest(response=response), patch.object(setup, "request_json", return_value=response), \
                    patch.object(setup, "put_json") as writes, self.assertRaises(ValueError):
                setup.setup_logs(BASE, template(), NOW)
            writes.assert_not_called()


class PutTests(unittest.TestCase):
    class Response:
        def __init__(self, value, status=200):
            self.value = value
            self.status = status

        def read(self, limit):
            return self.value[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_put_method_timeout_and_json_payload(self):
        with patch.object(setup.OPENER, "open", return_value=self.Response(b'{"acknowledged": true}')) as opened:
            setup.put_json(BASE + "/" + INDEX, {"a": False})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(json.loads(request.data), {"a": False})
        self.assertEqual(opened.call_args.kwargs["timeout"], 10)

    def test_put_rejects_status_bad_ack_json_size_and_nonfinite(self):
        for status, body in [(500, b'{}'), (302, b'{}'), (200, b'{"acknowledged": false}'),
                             (200, b'{"acknowledged": 1}'), (200, b'[]'), (200, b'bad json'),
                             (200, b'{"acknowledged": true, "bad": NaN}'),
                             (200, b'x' * (MAX_BYTES + 1))]:
            with self.subTest(status=status, size=len(body)), patch.object(
                setup.OPENER, "open", return_value=self.Response(body, status)
            ), self.assertRaises((ValueError, ProbeError)):
                setup.put_json(BASE + "/" + INDEX, {})

    def test_put_timeout_and_nonlocal_endpoint(self):
        with patch.object(setup.OPENER, "open", side_effect=TimeoutError("timeout")), self.assertRaises(ProbeError):
            setup.put_json(BASE + "/" + INDEX, {})
        with patch.object(setup.OPENER, "open") as opened, self.assertRaises(ValueError):
            setup.put_json("http://example.com/new", {})
        opened.assert_not_called()


class CliTests(unittest.TestCase):
    def test_config_size_and_shape_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr"):
            path = Path(directory) / "config.json"
            for content in [' ' * (512 * 1024 + 1), '[]', '{"opensearch_url":"http://example.com"}']:
                path.write_text(content, encoding="utf-8")
                with patch.object(setup, "setup_logs") as run:
                    self.assertEqual(setup.main(["--config", str(path)]), 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

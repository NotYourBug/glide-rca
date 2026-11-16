"""Create the fixed M0 log template and UTC index without replacing existing data."""

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from scripts.m0_probe import MAX_BYTES, OPENER, ProbeError, request_json, validate_url


TEMPLATE_NAME = "glide-m0-logs-v2"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "infra/otel-demo/opensearch-logs-v2-template.json"


def _object_json(content: bytes | str) -> dict:
    def reject_constant(value):
        raise ValueError("Nonfinite JSON constant")

    value = json.loads(content, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _read_json(path: Path) -> dict:
    if path.stat().st_size > 512 * 1024:
        raise ValueError("JSON file exceeds 512 KiB")
    return _object_json(path.read_text(encoding="utf-8-sig"))


def put_json(url: str, body: dict) -> dict:
    validate_url(url, query_allowed=True)
    request = Request(url, data=json.dumps(body, allow_nan=False).encode("utf-8"), method="PUT",
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "Accept-Encoding": "identity"})
    try:
        try:
            response = OPENER.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            content = response.read(MAX_BYTES + 1)
    except (URLError, OSError, TimeoutError) as error:
        raise ProbeError("transport_error", type(error).__name__) from error
    if len(content) > MAX_BYTES:
        raise ValueError("Response exceeded 10 MiB")
    if not 200 <= status < 300:
        raise ValueError(f"OpenSearch PUT failed: HTTP {status}")
    payload = _object_json(content)
    if payload.get("acknowledged") is not True:
        raise ValueError("OpenSearch did not acknowledge creation")
    return payload


def _declared_equal(expected, actual) -> bool:
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return all(key in actual and _declared_equal(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(_declared_equal(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _mapping(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid mapping object")
    result = copy.deepcopy(value)
    for key in ("properties", "fields"):
        if key in result:
            if not isinstance(result[key], dict):
                raise ValueError("Invalid mapping fields")
            result[key] = {name: _mapping(field) for name, field in result[key].items()}
    # OpenSearch may omit an object's explicit type in GET mapping responses.
    if ("properties" in result or "enabled" in result) and "type" not in result:
        result["type"] = "object"
    return result


def _index_mapping(value: dict) -> dict:
    result = _mapping(value)
    source = result.setdefault("_source", {})
    if not isinstance(source, dict):
        raise ValueError("Invalid source mapping")
    # GET mapping omits the default enabled source, even when PUT explicitly set it.
    source.setdefault("enabled", True)
    return result


def _settings(settings: dict) -> dict:
    if not isinstance(settings, dict) or not isinstance(settings.get("index", {}), dict):
        raise ValueError("Invalid template settings")
    if set(settings.get("index", {})) & (set(settings) - {"index"}):
        raise ValueError("Ambiguous template settings")
    normalized = {**settings.get("index", {}), **{k: v for k, v in settings.items() if k != "index"}}
    for key in ("number_of_shards", "number_of_replicas"):
        if key in normalized:
            item = normalized[key]
            if type(item) is int and item >= 0:
                normalized[key] = str(item)
            elif type(item) is not str or not item.isascii() or not item.isdecimal() or str(int(item)) != item:
                raise ValueError("Invalid shard or replica setting")
    return normalized


def _template(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("template"), dict):
        raise ValueError("Invalid index template")
    result = copy.deepcopy(value)
    body = result["template"]
    body["mappings"] = _index_mapping(body.get("mappings"))
    body["settings"] = _settings(body.get("settings", {}))
    if type(result.get("composed_of")) is list and not result["composed_of"]:
        result.pop("composed_of")
    if result.get("allow_auto_create") is True:
        result.pop("allow_auto_create")
    if type(body.get("aliases")) is dict and not body["aliases"]:
        body.pop("aliases")
    return result


def _existing(url: str, missing_type: str) -> dict | None:
    status, payload = request_json(url)
    if status == 404 and isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        if payload["error"].get("type") == missing_type:
            return None
    if status != 200 or not isinstance(payload, dict):
        raise ValueError(f"OpenSearch GET failed or returned an invalid object: HTTP {status}")
    return payload


def _verify_index(payload: dict, index: str, expected: dict) -> None:
    if set(payload) != {index} or not isinstance(payload[index], dict):
        raise ValueError("Index mapping response does not identify the expected index")
    actual = _index_mapping(payload[index].get("mappings"))
    if not (_declared_equal(expected, actual) and _declared_equal(actual, expected)):
        raise ValueError("Existing index mapping is incompatible; no replacement attempted")


def _verify_index_options(base_url: str, index: str, expected: dict) -> None:
    url = base_url + "/" + index
    payload = _existing(url + "/_settings?include_defaults=false", "index_not_found_exception")
    if payload is None or set(payload) != {index} or not isinstance(payload[index], dict):
        raise ValueError("Invalid index settings response")
    settings = _settings(payload[index].get("settings"))
    for key in ("creation_date", "uuid", "provided_name"):
        if key in settings:
            item = settings.pop(key)
            if not isinstance(item, str) or not item:
                raise ValueError("Invalid generated index metadata")
            if key == "provided_name" and item != index:
                raise ValueError("Unexpected provided index name")
            if key == "creation_date" and (not item.isascii() or not item.isdecimal()):
                raise ValueError("Invalid index creation date")
    if "version" in settings:
        version = settings.pop("version")
        if (not isinstance(version, dict) or not version or set(version) - {"created", "upgraded"}
                or any(not isinstance(v, str) or not v.isascii() or not v.isdecimal() for v in version.values())):
            raise ValueError("Invalid generated index version")
    if "replication" in settings and settings.pop("replication") != {"type": "DOCUMENT"}:
        raise ValueError("Unexpected index replication setting")
    for key in ("default_pipeline", "final_pipeline"):
        if key in settings and settings.pop(key) != "_none":
            raise ValueError("Existing index has an ingest pipeline")
    if "blocks" in settings:
        blocks = settings.pop("blocks")
        if (not isinstance(blocks, dict) or set(blocks) - {"write", "read", "read_only", "metadata", "read_only_allow_delete"}
                or any(value != "false" for value in blocks.values())):
            raise ValueError("Existing index has a block or unknown block setting")
    if not (_declared_equal(expected, settings) and _declared_equal(settings, expected)):
        raise ValueError("Existing index settings are incompatible; no replacement attempted")
    payload = _existing(url + "/_alias", "index_not_found_exception")
    if (payload is None or set(payload) != {index} or not isinstance(payload[index], dict)
            or type(payload[index].get("aliases")) is not dict or payload[index]["aliases"]):
        raise ValueError("Existing index has aliases or an invalid alias response")


def setup_logs(base_url: str, template: dict, now: datetime | None = None) -> dict:
    base_url = validate_url(base_url)
    expected_template = _template(template)
    expected_mapping = expected_template["template"]["mappings"]
    if (template.get("index_patterns") != ["otel-logs-v2-*"]
            or expected_mapping.get("_meta", {}).get("schema") != "glide-m0-logs.v2"):
        raise ValueError("Template must target the fixed M0 v2 schema and index prefix")
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Index date requires a timezone-aware timestamp")
    index = "otel-logs-v2-" + at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    template_url = base_url + "/_index_template/" + TEMPLATE_NAME
    existing_template = _existing(template_url, "resource_not_found_exception")
    if existing_template is not None:
        values = existing_template.get("index_templates")
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)
                or values[0].get("name") != TEMPLATE_NAME):
            raise ValueError("Invalid index template response")
        actual_template = _template(values[0].get("index_template"))
        if not (_declared_equal(expected_template, actual_template) and _declared_equal(actual_template, expected_template)):
            raise ValueError("Existing template is incompatible; no replacement attempted")
    index_url = base_url + "/" + index
    existing_index = _existing(index_url + "/_mapping", "index_not_found_exception")
    if existing_index is not None:
        _verify_index(existing_index, index, expected_mapping)
        _verify_index_options(base_url, index, expected_template["template"]["settings"])
    # Check both resources before creating either one.
    if existing_template is None:
        put_json(template_url + "?create=true", template)
    if existing_index is None:
        put_json(index_url, template["template"])
        created = _existing(index_url + "/_mapping", "index_not_found_exception")
        if created is None:
            raise ValueError("Created index was not found")
        _verify_index(created, index, expected_mapping)
        _verify_index_options(base_url, index, expected_template["template"]["settings"])
    return {"template": TEMPLATE_NAME, "index": index,
            "created_template": existing_template is None, "created_index": existing_index is None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = _read_json(args.config)
        endpoint = validate_url(config.get("opensearch_url"))
        result = setup_logs(endpoint, _read_json(TEMPLATE_PATH))
    except (OSError, ValueError, ProbeError) as error:
        print(f"Log index initialization failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

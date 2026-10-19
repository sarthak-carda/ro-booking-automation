"""Tests for reference-based generation: capture.load_reference / reference_from_path,
the Stage 2 prompt built from a reference, the pipeline's "funnel or reference" guard, and
the offline mock's refusal to fake a reference-only conversion.

No live LLM: `complete` is patched where the prompt itself is under test.
"""
import json
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.capture import MAX_REFERENCE_CHARS, load_reference, reference_from_path, validate_reference
from app.pipeline.convert import convert_funnel_to_script, repair_funnel_script

GEN_REQUEST = {
    "vendorName": "Example",
    "portalUrl": "http://example.test",
    "requiresCredentials": False,
    "bookingSchemaSample": {"bookingId": 1, "inventoryDate": "2026-11-05", "guestDetails": {"firstName": "Jordan"}},
}
PROMETHEUS_SCRIPT = 'const { values } = parseArgs({ options: { date: {type:"string"}, "first-name": {type:"string"} } });\nawait page.locator("#go").click();'
SAMPLE_OUTPUT = '{"selection": {"date": "2026-11-05"}, "fields": {"firstName": "Jordan"}}'


def _write_bundle(dir_path):
    (dir_path / "script.ts").write_text(PROMETHEUS_SCRIPT, encoding="utf8")
    (dir_path / "data.json").write_text(SAMPLE_OUTPUT, encoding="utf8")
    (dir_path / "README.md").write_text("# Prometheus collector", encoding="utf8")


class TestReferenceLoading:
    def test_unzipped_prometheus_bundle(self, tmp_path):
        _write_bundle(tmp_path)
        ref = reference_from_path(str(tmp_path))
        assert ref["kind"] == "prometheus-script"
        assert ref["content"] == PROMETHEUS_SCRIPT
        assert ref["sampleOutput"] == SAMPLE_OUTPUT  # README is ignored

    def test_zipped_prometheus_bundle_even_without_a_zip_extension(self, tmp_path):
        # The real TOSC bundle arrived as a file with no .zip extension.
        bundle = tmp_path / "prometheus-script-vive.example.com"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("script.ts", PROMETHEUS_SCRIPT)
            zf.writestr("data.json", SAMPLE_OUTPUT)
        ref = reference_from_path(str(bundle))
        assert ref["kind"] == "prometheus-script" and ref["content"] == PROMETHEUS_SCRIPT

    def test_devtools_recorder_export(self, tmp_path):
        export = tmp_path / "recording.json"
        export.write_text(json.dumps({"title": "x", "steps": [{"type": "click", "selectors": [["#a"]]}]}), encoding="utf8")
        ref = reference_from_path(str(export))
        assert ref["kind"] == "devtools-recorder"

    def test_wrong_files_fail_loudly(self, tmp_path):
        (tmp_path / "empty-dir").mkdir()
        with pytest.raises(ValueError, match="no script.ts"):
            reference_from_path(str(tmp_path / "empty-dir"))
        not_a_recording = tmp_path / "other.json"
        not_a_recording.write_text('{"hello": 1}', encoding="utf8")
        with pytest.raises(ValueError, match="Recorder"):
            reference_from_path(str(not_a_recording))

    def test_validate_rejects_bad_kind_empty_and_oversized(self):
        with pytest.raises(ValueError, match="invalid kind"):
            validate_reference({"kind": "nope", "content": "x"})
        with pytest.raises(ValueError, match="non-empty"):
            validate_reference({"kind": "other", "content": "  "})
        with pytest.raises(ValueError, match="wrong file"):
            validate_reference({"kind": "other", "content": "x" * (MAX_REFERENCE_CHARS + 1)})

    def test_load_reference_none_and_dict(self):
        assert load_reference(None) is None
        assert load_reference({"kind": "other", "content": "x"})["kind"] == "other"


@pytest.mark.asyncio
async def test_prompt_with_reference_only_includes_it_and_omits_the_funnel_section():
    reference = {"kind": "prometheus-script", "content": PROMETHEUS_SCRIPT, "sampleOutput": SAMPLE_OUTPUT,
                 "fieldMapping": {"--first-name": "guestDetails.firstName"}}
    captured = {}

    async def fake_complete(system, user_content, purpose):
        captured.update(system=system, text=user_content[0]["text"], purpose=purpose)
        return {"text": "```js\nwindow.HeadoutAutomation = {};\n```", "mocked": False}

    with patch("app.pipeline.convert.complete", new=fake_complete):
        result = await convert_funnel_to_script(GEN_REQUEST, None, reference)

    assert result["script"] == "window.HeadoutAutomation = {};"
    assert captured["purpose"] == "convert"
    text = captured["text"]
    assert PROMETHEUS_SCRIPT in text and SAMPLE_OUTPUT in text and "guestDetails.firstName" in text
    assert "Reference (kind: prometheus-script)" in text
    assert "No funnel recording was captured" in text and "## Funnel recording" not in text
    # The contract carries the translation rules and the lessons learned on real vendors.
    assert "REFERENCE MATERIAL" in captured["system"] and "nativeClick" in captured["system"]
    # Reference content must not sit in a ```json fence (the offline mock merges those blocks).
    assert "```json\n" + PROMETHEUS_SCRIPT not in text


@pytest.mark.asyncio
async def test_convert_needs_at_least_one_of_funnel_or_reference():
    with pytest.raises(ValueError, match="funnel recording, a reference"):
        await convert_funnel_to_script(GEN_REQUEST, None, None)


@pytest.mark.asyncio
async def test_repair_prompt_carries_reference_failing_script_error_and_dom():
    captured = {}

    async def fake_complete(system, user_content, purpose):
        captured.update(text=user_content[0]["text"], purpose=purpose)
        return {"text": "```js\nfixed\n```", "mocked": False}

    reference = {"kind": "prometheus-script", "content": PROMETHEUS_SCRIPT}
    with patch("app.pipeline.convert.complete", new=fake_complete):
        await repair_funnel_script(GEN_REQUEST, None, "BROKEN SCRIPT", "Step 3 failed", "<html>dom</html>", reference)

    assert captured["purpose"] == "repair"
    for needle in (PROMETHEUS_SCRIPT, "BROKEN SCRIPT", "Step 3 failed", "<html>dom</html>"):
        assert needle in captured["text"]


@pytest.mark.asyncio
async def test_offline_mock_refuses_reference_only_conversion():
    from app.llm.mock import mock_complete
    prompt = [{"type": "text", "text": "## Booking JSON schema\n```json\n{}\n```\n## Reference\n`````text\nx\n`````"}]
    with pytest.raises(ValueError, match="reference-only"):
        mock_complete("convert", prompt)


@pytest.mark.asyncio
async def test_pipeline_requires_a_funnel_or_a_reference(monkeypatch, tmp_path):
    from app import config as config_module
    from app import db as db_module
    from app.pipeline.run_pipeline import run_vendor_pipeline

    monkeypatch.setattr(config_module.config, "data_dir", str(tmp_path / "data") + "/")
    db_module._cache.clear()
    vendor = {"id": "ref-test", "name": "Example", "portalUrl": "http://example.test", "requiresCredentials": False}
    with pytest.raises(ValueError, match="funnel recording or a reference"):
        await run_vendor_pipeline(vendor=vendor, sample_booking={"bookingId": 1})


@pytest.mark.asyncio
async def test_reference_only_pipeline_skips_the_narrow_patch_and_passes_the_reference_to_repair(monkeypatch, tmp_path):
    """With no funnel there are no step indices, so repair must go straight to the broad
    path, handing the model the reference."""
    from app import config as config_module
    from app import db as db_module
    import app.pipeline.run_pipeline as rp

    monkeypatch.setattr(config_module.config, "data_dir", str(tmp_path / "data") + "/")
    db_module._cache.clear()
    seen = {}

    async def fake_convert(generation_request, funnel, reference=None):
        seen["convert_args"] = (funnel, reference)
        return {"script": "S0", "mocked": False}

    audits = iter([
        {"pass": False, "status": "ERROR", "error": 'Step 0 (click): no element matched candidates ["#x"]', "dom": "<html></html>"},
        {"pass": True, "status": "PAYMENT_REQUIRED", "error": None},
    ])

    async def fake_audit(*, script, booking, credentials=None, portal_url):
        return next(audits)

    async def fake_broad_repair(**kwargs):
        seen["repair_kwargs"] = kwargs
        return {"script": "S1", "mocked": False}

    async def must_not_run(**kwargs):
        raise AssertionError("narrow patch must not run without a funnel")

    monkeypatch.setattr(rp, "convert_funnel_to_script", fake_convert)
    monkeypatch.setattr(rp, "audit_script", fake_audit)
    monkeypatch.setattr("app.pipeline.repair.audit_script", fake_audit)
    monkeypatch.setattr("app.pipeline.repair.diagnose_drift", must_not_run)
    monkeypatch.setattr("app.pipeline.repair.repair_funnel_script", fake_broad_repair)
    monkeypatch.setattr("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0)

    vendor = {"id": "ref-test-2", "name": "Example", "portalUrl": "http://example.test", "requiresCredentials": False}
    reference = {"kind": "prometheus-script", "content": PROMETHEUS_SCRIPT}
    result = await rp.run_vendor_pipeline(
        vendor=vendor, sample_booking={"bookingId": 1, "guestDetails": {}}, reference=reference, auto_ship=False,
    )

    assert result["status"] == "awaiting_review" and result["script"] == "S1"
    assert seen["convert_args"] == (None, reference)
    assert seen["repair_kwargs"]["reference"] == reference


def test_compact_dom_strips_noise_and_keeps_the_interactive_markup():
    from app.pipeline.convert import MAX_REPAIR_DOM_CHARS, _compact_dom
    dom = (
        "<html><head><meta charset=utf-8><link rel=stylesheet href=x.css><style>.a{color:red}</style>"
        "<script>var big = 'x'.repeat(100000);</script></head><body><!-- tracking -->"
        "<svg><path d='M0'/></svg><div class='ui-datepicker-month'>June</div>   <a data-date='5'>5</a>"
        "</body></html>"
    )
    out = _compact_dom(dom)
    assert "ui-datepicker-month" in out and "data-date='5'" in out
    for gone in ("<script", "<style", "<svg", "tracking", "<link", "<meta"):
        assert gone not in out
    assert len(_compact_dom("<p>" + "a" * 200_000 + "</p>")) < MAX_REPAIR_DOM_CHARS + 100


@pytest.mark.asyncio
async def test_repair_prompt_tells_the_model_to_check_page_state_before_editing_the_failing_step():
    captured = {}

    async def fake_complete(system, user_content, purpose):
        captured["text"] = user_content[0]["text"]
        captured["system"] = system
        return {"text": "```js\nfixed\n```", "mocked": False}

    with patch("app.pipeline.convert.complete", new=fake_complete):
        await repair_funnel_script(GEN_REQUEST, None, "S", "Step 1 failed", "<html><script>noise()</script><p>June</p></html>",
                                   {"kind": "prometheus-script", "content": PROMETHEUS_SCRIPT})

    assert "only where the problem SURFACED" in captured["text"]
    assert "noise()" not in captured["text"] and "<p>June</p>" in captured["text"]
    assert "REFERENCE'S SELECTORS AND ATTRIBUTE PLACEMENT WIN" in captured["system"]
    assert "DEFINE EACH SELECTOR ONCE" in captured["system"]

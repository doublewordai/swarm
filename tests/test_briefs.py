import pytest

from src import briefs


def test_registry_has_builtins():
    names = briefs.list_briefs()
    assert "audit" in names and "onboarding" in names


def test_get_brief_shapes():
    audit = briefs.get_brief("audit")
    assert audit.result_key == "findings" and audit.verifier_prompt  # audit verifies
    onboarding = briefs.get_brief("onboarding")
    assert onboarding.result_key == "sections" and onboarding.verifier_prompt is None  # no verify


def test_unknown_brief_raises():
    with pytest.raises(KeyError):
        briefs.get_brief("nope")


def test_result_schemas_are_objects():
    for name in briefs.list_briefs():
        b = briefs.get_brief(name)
        assert b.result_schema["type"] == "object"
        assert isinstance(b.worker_tools, tuple)

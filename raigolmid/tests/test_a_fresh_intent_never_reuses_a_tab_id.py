"""Tab ids are never reused. The counter lives in the intent, but homes and archives
outlive it: an intent removed or refused as an old format must not number tabs from 1 again."""
from raigolmid.intent import IntentStore


def test_a_fresh_intent_numbers_past_every_home_and_archive(tmp_path):
    store = IntentStore(tmp_path / "intent.json")
    intent = store.load(["manager", "tab-7", "tab-4-20260925T063806",
                         "tab-4-20260925T063806.json", "tab-12-20260925T090741.2"])
    assert intent.new_tab_id() == "tab-13"



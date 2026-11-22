"""Built-in behaviours (cloud intents) can only be the ANSWER to a fresh voice request."""
from app import turn


def test_claim_after_hey_vector():
    turn.reset(is_followup=False, voice_turn=True)
    assert turn.claim_intent("intent_play_fistbump")
    assert turn.pending_intent == "intent_play_fistbump"
    assert not turn.claim_intent("intent_play_anytrick")  # one answer per request


def test_no_claim_in_followup_listening():
    turn.reset(is_followup=True, voice_turn=True)
    assert not turn.claim_intent("intent_play_fistbump")


def test_no_claim_after_he_spoke():
    turn.reset(is_followup=False, voice_turn=True)
    turn.mark_wirepod_control()
    assert not turn.claim_intent("intent_play_fistbump")


def test_no_claim_outside_voice_turns():
    turn.reset(via="a2a")
    assert not turn.claim_intent("intent_play_fistbump")
    turn.reset()  # background routines/schedules use the defaults
    assert not turn.claim_intent("intent_play_fistbump")


def test_slot_closes_at_turn_end():
    turn.reset(is_followup=False, voice_turn=True)
    turn.close_intent_slot()
    assert not turn.claim_intent("intent_play_fistbump")

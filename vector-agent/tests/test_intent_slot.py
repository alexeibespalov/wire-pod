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


def test_speech_estimate_queues_back_to_back():
    import time
    turn.reset(is_followup=False, voice_turn=True)
    t0 = time.time()
    turn.mark_speech("Let me see.")                        # ~0.8 get-in + 0.3 + 3 words / 2.5
    first = turn.speech_until - t0
    assert 1.5 < first < 3.5
    turn.mark_speech("{{playAnimationWI||happy}}")         # cues aren't words
    assert turn.speech_until - t0 == first
    turn.mark_speech("Hmm, I can't quite make it out.")   # queued after the first sentence
    assert turn.speech_until - t0 > first + 2
    turn.reset()
    assert turn.speech_until == 0.0

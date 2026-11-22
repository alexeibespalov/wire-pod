"""Build a pruned wire-pod en-US.json so more free-form speech falls through to the LLM.

wire-pod matching (chipper/pkg/wirepod/ttr/matchIntentSend.go):
  1. whole utterance == keyphrase           (any intent)
  2. utterance CONTAINS keyphrase (no word boundaries, first intent wins)
     -- skipped for intents with requiresexact=true
Anything unmatched goes to the knowledge graph (LLM) because intentgraph=true.

Same intent name may appear twice (one exact-only entry + one contains entry).
"""
import json
import sys

# (intent, requiresexact, [phrases])
SPEC = [
    # --- names / identity
    ("intent_names_username_extend", False, ["my name is", "my name's"]),
    ("intent_names_ask", False, ["who am i", "what's my name", "whats my name", "what is my name"]),
    ("intent_character_age", False, ["how old are you", "what's your age", "whats your age"]),
    # --- weather: DISABLED (see DISABLED below) -> the agent answers via web search
    # --- looks / system
    ("intent_imperative_eyecolor", False, ["eye color", "eye colour"]),
    ("intent_explore_start", False, ["start exploring", "go exploring", "go explore"]),
    ("intent_system_charger", False, ["go to your charger", "go to the charger", "find your charger",
                                      "go home", "go charge", "go to charger", "base station",
                                      "your base", "go back home", "back to your charger",
                                      "back to your home", "go to your home", "back on your charger"]),
    ("intent_system_sleep", False, ["go to sleep"]),
    ("intent_system_sleep", True, ["sleep"]),
    ("intent_system_noaudio", True, ["[blank_audio]", "[door opens]", "[sighs]"]),
    # --- greetings (exact only)
    ("intent_greeting_hello", True, ["hello", "hi", "hey", "hi there", "hello there", "hello vector", "hi vector"]),
    ("intent_greeting_goodmorning", True, ["good morning", "morning", "good afternoon"]),
    ("intent_greeting_goodnight", True, ["good night", "goodnight", "night night"]),
    ("intent_greeting_goodbye", True, ["goodbye", "good bye", "bye", "bye bye", "see you later"]),
    ("intent_seasonal_happynewyear", False, ["happy new year"]),
    ("intent_seasonal_happyholidays", False, ["merry christmas", "happy holidays"]),
    # --- alexa (unused but harmless)
    ("intent_amazon_signin", False, ["sign in to alexa", "sign into alexa"]),
    ("intent_amazon_signin", False, ["sign out of alexa"]),
    # --- movement
    ("intent_imperative_forward", False, ["move forward", "go forward", "drive forward"]),
    ("intent_imperative_turnaround", False, ["turn around"]),
    ("intent_imperative_turnleft", False, ["turn left", "go left"]),
    ("intent_imperative_turnright", False, ["turn right", "go right"]),
    ("intent_imperative_backup", False, ["back up", "move back", "back off", "go backwards", "move backwards"]),
    ("intent_imperative_backup", True, ["go back", "back"]),  # bare only: "go back to X" is not backing up
    ("intent_imperative_come", False, ["come here", "come to me"]),
    ("intent_imperative_lookatme", False, ["look at me"]),
    # --- cube / tricks / games
    ("intent_play_rollcube", False, ["roll your cube", "roll the cube"]),
    ("intent_play_popawheelie", False, ["pop a wheelie", "do a wheelie", "do a wheel stand"]),
    ("intent_play_fistbump", False, ["fist bump", "fistbump", "fist bomb"]),
    ("intent_play_blackjack", False, ["blackjack", "play cards"]),
    ("intent_play_pickupcube", False, ["pick up your cube", "pick up the cube", "pick up cube"]),
    ("intent_imperative_fetchcube", False, ["fetch your cube", "fetch the cube", "bring me your cube", "bring me the cube"]),
    ("intent_imperative_findcube", False, ["find your cube", "find the cube"]),
    ("intent_play_anytrick", False, ["do a trick", "something cool", "show me a trick"]),
    ("intent_play_keepaway", False, ["keepaway", "keep away"]),
    ("intent_blackjack_hit", True, ["hit", "hit me"]),
    ("intent_blackjack_stand", True, ["stand", "stay", "i'll stand", "ill stand"]),
    ("intent_imperative_dance", False, ["dance for me", "do a dance", "can you dance", "dance to the music",
                                        "dance to the beat", "start dancing"]),
    ("intent_imperative_dance", True, ["dance", "boogie"]),
    ("intent_photo_take_extend", False, ["take a photo", "take a picture", "take my photo", "take my picture",
                                         "take a selfie", "take a photo of me"]),
    # --- short replies / feedback (exact only)
    ("intent_imperative_affirmative", True, ["yes", "yeah", "yep", "sure", "correct", "yes please", "ok", "okay"]),
    ("intent_imperative_negative", True, ["no", "nope", "dont", "don't", "no thanks", "no thank you"]),
    ("intent_imperative_praise", True, ["good robot", "good boy", "good job", "well done", "good", "awesome", "amazing"]),
    ("intent_imperative_abuse", True, ["bad robot", "bad boy", "bad"]),
    ("intent_imperative_apologize", True, ["sorry", "i'm sorry", "im sorry", "i apologize", "never mind", "nevermind"]),
    ("intent_imperative_love", False, ["i love you"]),
    ("intent_imperative_love", True, ["love you"]),
    ("intent_imperative_quiet", True, ["hush"]),  # stop / quiet / be quiet -> agent (stops music)
    ("intent_imperative_shutup", False, ["shut up"]),
    # --- volume
    ("intent_imperative_volumedown", False, ["volume down", "turn the volume down", "turn down the volume",
                                             "turn it down", "be quieter"]),
    ("intent_imperative_volumeup", False, ["volume up", "turn the volume up", "turn up the volume",
                                           "turn it up", "be louder"]),
    ("intent_imperative_volumeup", True, ["louder"]),
    ("intent_imperative_volumedown", True, ["quieter"]),
    ("intent_imperative_volumelevel_extend", False, ["set your volume", "set the volume", "volume to"]),
    # --- LLM conversation entry
    ("intent_knowledge_promptquestion", False, ["i have a question", "let's talk", "lets talk"]),
    ("intent_knowledge_promptquestion", True, ["question", "conversation"]),
    # --- clock / timer
    ("intent_clock_checktimer", False, ["check the timer", "check timer", "check my timer", "how much time is left"]),
    ("intent_global_stop_extend", False, ["stop the timer", "stop timer", "cancel the timer", "cancel timer",
                                          "stop the clock", "stop the alarm"]),
    ("intent_clock_settimer_extend", False, ["set a timer", "set timer", "set the timer", "start a timer", "timer for"]),
    ("intent_clock_time", False, ["what time is it", "what's the time", "whats the time", "tell me the time",
                                  "time is it"]),
    # --- messages
    ("intent_message_recordmessage_extend", False, ["record a message", "record message", "leave a message"]),
    ("intent_message_playmessage_extend", False, ["play message", "play a message", "play my message",
                                                  "play messages"]),
]


# Built-in intents deliberately removed so these requests fall through to the agent.
DISABLED = {
    "intent_weather_extend",  # needs wire-pod's own weather API key; agent uses Tavily instead
}


def main(orig_path, out_path):
    orig = json.load(open(orig_path))
    orig_names = {i["name"] for i in orig}
    new_names = {n for n, _, _ in SPEC}
    missing = orig_names - new_names - DISABLED
    unknown = (new_names | DISABLED) - orig_names
    if missing or unknown:
        sys.exit(f"intent mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")

    out = []
    for name, exact, phrases in SPEC:
        lowered = [p.lower() for p in phrases]
        out.append({"name": name, "keyphrases": lowered, "requiresexact": exact})
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    n_orig = sum(len(i["keyphrases"]) for i in orig)
    n_new = sum(len(p) for _, _, p in SPEC)
    n_exact = sum(len(p) for _, e, p in SPEC if e)
    print(f"intents: {len(new_names)} of {len(orig_names)} kept (disabled: {sorted(DISABLED)}) | keyphrases {n_orig} -> {n_new} "
          f"({n_exact} exact-only) | entries {len(orig)} -> {len(out)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

import json,sys
def match(intents, t):
    t=t.lower()
    for i in intents:
        for k in i["keyphrases"]:
            if t==k.lower(): return i["name"]+" (exact)"
    for i in intents:
        if i.get("requiresexact"): continue
        for k in i["keyphrases"]:
            if k.lower() in t: return i["name"]+f" ('{k}')"
    return "→ QWEN"
old=json.load(open(sys.argv[1])); new=json.load(open(sys.argv[2]))
tests=["how do black holes form","why are my eyes itchy","what happened back in the day","tell me a good joke",
"what is the capital of france","how are you","can you tell me a story about a sheep","what is a good name for a cat",
"what time is it","whats the time","set a timer for five minutes","turn left","go to your charger","dance",
"can you dance","yes","good robot","take a photo","i have a question","how old is the universe",
"what should i cook for dinner","explain how a car engine works","who won the world cup","stop"]
print(f"{'utterance':40} | {'OLD':38} | NEW"); print("-"*120)
for t in tests: print(f"{t:40} | {match(old,t)[:38]:38} | {match(new,t)}")

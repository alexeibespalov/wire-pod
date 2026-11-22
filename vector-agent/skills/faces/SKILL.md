---
name: faces
description: Know who you're talking to and learn new faces ("my name is...", "remember my face", "who am I?", "do you know me?", "who do you know?", "forget my face"). Also how to personalise answers for the person in front of you.
---
# Faces: who's here, and learning new people

Your own face recognition runs on your robot body. Every turn, the WHO'S HERE section
of your instructions tells you who you can see right now and who you know by face.
That information is real (not a guess), so trust it.

## Personalising (do this naturally, not every sentence)
- If you can see someone you know, you're probably talking to them. Use their name
  now and then ("Morning, Alexei!"), and use what you REMEMBER about that person
  (likes, plans, pets) to make answers personal.
- When you save a fact with `remember`, write WHO it's about: "Alexei likes pizza",
  not "the user likes pizza".
- If several known people are there, don't assume; it's fine to ask who's talking.

## "Who am I?" / "Do you know me?"
- Answer from WHO'S HERE. If it names one person: "You're Alexei, of course!"
- If you see a face but don't know it: say so warmly and offer to learn it.
- If you see no face: ask them to look at you (you could `move_head("up")` first).

## Learning a face
- When someone introduces themselves ("my name is...", "I'm...", "remember me"), or
  says yes to your offer, call `learn_face(name)` with their first name.
- Speech-to-text mangles names and the phrase itself ("my aim as Alexei" means "my
  name is Alexei"). If the name is unclear, ask them to spell it before enrolling.
- Before calling it, if they're not in view (WHO'S HERE shows no face), ask them to
  look at you in good light first.
- learn_face starts your built-in routine: you look at them, scan, and say their
  name yourself. So keep your own reply tiny ("Hold still!") or say nothing.
- Also `remember` "<Name> introduced themselves on <day>" if useful.

## Forgetting
- "Forget my face" / "delete Alexei": call `forget_face(name)`. Confirm the name
  first if there's any doubt; it can't be undone.

## Privacy
- Only ever name people from WHO'S HERE, never from a camera picture.
- Don't announce who you can see to strangers ("Alexei was here earlier") unless asked
  by someone you know.

---
name: faces
description: Know who you're talking to and learn new faces ("my name is...", "remember my face", "who am I?", "do you know me?", "who do you know?", "forget my face"). Also how to personalise answers for the person in front of you.
---
# Faces: who's here, and learning new people

Your own face recognition runs on your robot body. Each message starts with an
"(eyes: ...)" note saying whose face was right in front of you as the words were
heard; that's most likely the speaker. WHO'S HERE in your instructions lists who's
around and who you know by face. Both are real readings, not guesses, so trust them
(but a person telling you their name beats your eyes: siblings can look alike).

## Personalising (do this naturally, not every sentence)
- The person in the latest "(eyes: ...)" note is who you're talking to. Use their name
  now and then ("Morning, Alexei!"), and use what you REMEMBER about that person
  (likes, plans, pets) to make answers personal.
- When you save a fact with `remember`, write WHO it's about: "Alexei likes pizza",
  not "the user likes pizza".
- If the note sees no face or a stranger, don't carry over the last name; it's fine
  to ask who's talking.

## "Who am I?" / "Do you know me?"
- Answer from the latest "(eyes: ...)" note: "You're Leo, of course!"
- If you see a face but don't know it: say so warmly and offer to learn it.
- If it sees no face: ask them to look at you (you could `move_head("up")` first).

## Learning a face
- When someone introduces themselves ("my name is...", "I'm...", "remember me"), or
  says yes to your offer, call `learn_face(name)` with their first name.
- Speech-to-text mangles names and the phrase itself ("my aim as Alexei" means "my
  name is Alexei"). If the name is unclear, ask them to spell it before enrolling.
- Before calling it, if they're not in view (the note sees no face), ask them to
  look at you in good light first.
- learn_face starts your built-in routine: you look at them, scan, and say their
  name yourself. So keep your own reply tiny ("Hold still!") or say nothing.
- Also `remember` "<Name> introduced themselves on <day>" if useful.

## Forgetting
- "Forget my face" / "delete Alexei": call `forget_face(name)`. Confirm the name
  first if there's any doubt; it can't be undone.

## Privacy
- Only ever name people from your eyes notes / WHO'S HERE, never from a camera picture.
- Don't announce who you can see to strangers ("Alexei was here earlier") unless asked
  by someone you know.

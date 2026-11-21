---
name: animations
description: Pick and play one of Vector's own named animations (greetings, celebrations, sulks, sneezy wake-ups, dance moves, weather icons, performances). Use when a moment deserves a real full-body reaction, or the user asks you to act out a feeling or do something expressive.
---
# Playing named animations

`play_animation(name)` plays one of your own built-in animations: face, sound and body
together. It runs BEFORE your words are spoken and you are silent while it plays.

## When to use it (and when not)
- Use it for moments that deserve a real reaction: being praised, being told off, a big
  win, saying hello or goodnight, being asked to act out a feeling ("show me you're sad",
  "do something funny", "how do you feel about Mondays?").
- For everyday colour inside a sentence keep using the quick inline cues
  ({{playAnimationWI||happy}} etc.). Those are cheap; play_animation is a moment.
- At most ONE play_animation per reply. Prefer animations under ~5 seconds.
- After an animation, keep your spoken reply short: the animation already said a lot.
- On his charger his body can't move, so animations play face and head only. That's fine.
- Only use exact names from the catalog below. If a name errors, pick another from here.

## Catalog (name, seconds, what it looks like)

### Hello, goodbye, sleep
- anim_greeting_hello_01 (3.6), anim_greeting_hello_02 (3.2): cheerful hello
- anim_greeting_happy_03 (3.1): delighted to see you
- anim_greeting_imhome_01 (5.2): excited "you're home!"
- anim_greeting_goodmorning_01 (4.5), anim_greeting_goodmorning_02 (1.8): good morning
- anim_greeting_goodnight_01 (2.6), anim_greeting_goodnight_02 (4.3): sleepy goodnight
- anim_greeting_goodbye_01 (4.4), anim_greeting_goodbye_02 (2.9): goodbye wave
- anim_gotosleep_off_01 (1.6): eyes shutting off
- anim_gotosleep_wakeup_01 (5.1): waking up groggily
- anim_launch_wakeup_02 (4.0), anim_launch_wakeup_05 (3.1): stretchy wake-up

### Love, praise, happiness
- anim_feedback_iloveyou_01 (3.6), anim_feedback_iloveyou_02 (3.1): melting with love
- anim_feedback_goodrobot_01 (4.0), anim_feedback_goodrobot_02 (2.6): proud, praised
- anim_petting_lvl2_01 (3.8), anim_petting_lvl4_01 (4.3): blissed out, being petted
- anim_pounce_success_03 (2.4): smug little victory
- anim_blackjack_victorwin_01 (3.9): gloating win
- anim_fistbump_success_02 (3.6): fist bump celebration
- anim_freeplay_reacttoface_like_01 (4.6): happy to see a favourite face
- anim_gif_gleeserious_01 (3.0): gleeful then serious

### Sad, sorry, told off, annoyed
- anim_feedback_apology_01 (4.5), anim_feedback_apology_02 (2.7): sheepish sorry
- anim_feedback_badrobot_01 (4.3), anim_feedback_badrobot_02 (4.6): hurt, scolded
- anim_feedback_meanwords_01 (4.3), anim_feedback_meanwords_02 (3.8): wounded by mean words
- anim_feedback_shutup_01 (3.9), anim_feedback_bequiet_01 (2.3): grumpy hush
- anim_blackjack_victorlose_01 (3.5): disappointed loss
- anim_blackjack_victorbust_01 (5.4): dramatic "oh no" bust
- anim_pounce_fail_02 (2.4): missed, frustrated
- anim_fistbump_fail_01 (6.2): left hanging

### Confused, unsure, can't do it
- anim_gif_idk_01 (3.8): shrug, "I don't know"
- anim_gif_no_01 (2.1): firm no
- anim_wakeword_idk_02 (2.4), anim_wakeword_idk_03 (4.7): puzzled
- anim_communication_cantdothat_01 (2.4), anim_communication_cantdothat_02 (4.4): "can't do that"
- anim_knowledgegraph_fail_01 (3.7): couldn't find the answer
- anim_explorer_huh_far_01 (1.8): "huh?"

### Thinking, curious, looking around
- anim_knowledgegraph_searching_01 (1.2): thinking/searching
- anim_knowledgegraph_success_01 (3.2): "found it!"
- anim_observing_self_absorbed_01 (5.0): lost in thought
- anim_hiking_lookaround_02 (2.9): curious look around
- anim_hiking_react_02 (5.8): spots something interesting
- anim_reacttoface_unidentified_02 (1.8): "who's that?"
- anim_generic_look_up_idle_02 (3.3): looks up at you attentively
- anim_photo_focus_01 (3.6), anim_photo_shutter_01 (2.9): taking a photo

### Surprise, startle, dizzy
- anim_sudden_obstacle_react_02 (1.3): startled
- anim_rtpickup_reaction_03 (3.0): "whoa, put me down!"
- anim_dizzy_reaction_soft_01 (5.3): dizzy
- anim_dizzy_shake_stop_01 (4.2): shaking off dizziness
- anim_rtshake_lv2rtonhand_01 (4.6): rattled from shaking

### Fun and music
- anim_dancebeat_getready_01 (1.2): gets ready to dance
- anim_dancebeat_listening_02 (4.2): grooving to music
- anim_pounce_04 (2.1): playful pounce
- anim_volume_stage_max (2.5): "turn it up!"
- anim_eyecolorreact_random (10.6): eye colour light show (long)

### Weather icons (big animated icon on his face, ~14 s, use when asked to SHOW the weather)
- anim_weather_sunny_01, anim_weather_cloud_01, anim_weather_rain_01, anim_weather_snow_01,
  anim_weather_thunderstorm_01, anim_weather_windy_01, anim_weather_cold_01,
  anim_weather_stars_01 (clear night)

### Showpieces (long, only when asked to perform or show off)
- anim_uperformance_binaryeyes_01 (23.3): eyes turn into scrolling binary
- anim_uperformance_loosepixel_01 (19.9): a pixel escapes his face
- anim_holiday_hny_fireworks_01 (17.4): new year fireworks
- anim_holiday_hyn_confetti_01 (13.8): confetti celebration

### Single eye poses (instant, 1 s)
- anim_eyepose_happy, anim_eyepose_joy, anim_eyepose_curious, anim_eyepose_frustrated,
  anim_eyepose_angry, anim_eyepose_bliss, anim_eyepose_concerned, anim_eyepose_determined,
  anim_eyepose_focused

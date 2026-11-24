# Talking-head effects look book

Reference of the 51 starter effects from
[claude-video-effects](https://github.com/Vibha-Ramprakash/claude-video-effects)
(MIT, Vibha Ramprakash / Hop Studio).

This folder is a **look book**, not their renderer. Their Claude Code skill
bakes a new MP4 with face tracking. Studio places a named look on the
timeline (overlay behind you, or a punch zoom) and you still click Render.

Ask the co-editor by the wired names below. Quoted words go on screen.

## Wired (ask for these)

| Say this | What lands |
|---|---|
| `lean in` | Punch zoom on the speaker |
| `backdrop word "GO"` | Giant word drops in behind you |
| `spotlight` / `kill the lights` | Room goes dark, you stay lit; optional quoted word behind |
| `reject stamp` | Red stamp slams on |
| `comment box "nice"` | Instagram-style comment types itself |
| `todo list "one, two, three"` | Checklist beside / behind you |
| `huge strike "NO"` | Huge word behind you, then a red line through it |
| `head orbit` / `orbit around me` | Cards circle; they sit behind you (not the Remotion photo carousel) |

## The other 43

They are listed in `looks.json` so the co-editor can recognise the name and
say it is not wired yet. Do not dump their `fx.py` in here. To wire another
one, add `"wired": true` and a phrase, then a Studio port.

Hop Studio (paid, 220+ looks) stays off this shelf.

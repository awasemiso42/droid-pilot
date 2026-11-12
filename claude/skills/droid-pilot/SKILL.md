---
name: droid-pilot
description: Drive an Android emulator or test device with the `dp` command to test and debug an app — build/install/launch, read a short numbered UI tree, tap/type/swipe/draw, read only the app's own crashes and errors, and save the steps as a replayable regression flow. Use whenever you need to run, look at, operate or check an Android app on a device, instead of calling adb directly.
---

# droid-pilot (`dp`)

`dp` is the only way to touch Android devices here. Do not run `adb`, `emulator` or Gradle `install*` tasks yourself (a hook refuses them): dp keeps the output short, applies the safety rules and records every step.

## Rules

- The first output line is `OK …`, `NG …` or `BLOCKED <rule>: …`.
- **BLOCKED means stop.** Do not look for a way around it. Tell the user what was refused and why.
- `needs-human`: the button sends, buys, invites, shares or publishes something outside the device. Only when the user's task really needs it, run the same command again with `--yes`. Claude Code then asks the user to confirm.
- Device kinds: `test` (a real device the user registered as a test device) and `emu` (an emulator) may be changed freely. `safe` (any other real device) is read-only. Never run `dp register` on your own initiative.
- On a real device: if it is locked or asleep dp says so (`BLOCKED locked`): ask the user to unlock it; never type a PIN or password.
- Some installs need a person: MIUI (Xiaomi) shows an "USB経由でインストール" / "Install via USB" prompt on the device and refuses after a few seconds. Never try to press it. Tell the user what dp printed (usually: run `dp setup` at the device and press インストール).
- Typing into a password field is `BLOCKED needs-human`. Use `--yes` only for a test account's value the user gave you.

## The loop

```
dp up                      # build → install → launch → screen → app errors since launch
dp tap 6                   # press element [6]; prints what changed afterwards
dp screen                  # numbered tree again, when you need it
dp log                     # the app's crashes / errors since launch
# fix the code, then
dp up
```

`dp up --no-build` reuses the last APK. `dp build` only builds and prints `file:line: message` for each error.

## Looking at the screen

- `dp screen`: numbered elements in reading order. Elements on one line share a row on screen. Coordinates are in "shot space", the same pixels as the default screenshot.
  - Example lines: `[3] btn "設定" @936,32`, `[5] list 24件 @0,64-1024,560`, `[7] canvas PageView @…`.
  - Lists show their first 8 items. `--all` shows everything, `--find 文字` filters.
- Screenshots are saved to a file and only the path is printed. Read it with the Read tool only when the tree is not enough, e.g. for canvas content, layout or colours.
  - `dp screen --mark`: a screenshot with the element numbers drawn on it.
  - `dp shot --grid`: a screenshot with coordinate lines every 10%, for picking points on a canvas.
  - `dp shot --region x,y,w,h`: a full-resolution crop for small details.
- After every action dp prints one of:
  - `→ A → B` with the new screen (the screen changed to another activity)
  - `→ 同じ画面、変化 N か所` with `+` and `-` lines (the same screen changed)
  - `→ 変化なし` (nothing changed: the tap probably missed, or the app ignored it)

## Targets

| Form | Meaning |
|---|---|
| `4` | Number from the last `dp screen` (re-checked; `NG stale` means look again) |
| `text:新しいノート` | Exact label |
| `text~:ノート` | Partial label |
| `id:new_note` | resource-id, or a Compose testTag when the app sets `testTagsAsResourceId` |
| `desc:戻る` | contentDescription |
| `@512,300` | Shot-space pixels |
| `@50%,40%` | Fraction of the screen |
| `…#2` | The 2nd match |

## Actions

```
dp tap TARGET [--yes]          dp long TARGET [--ms 1200]
dp type "化学 abc" [--into TARGET] [--clear] [--enter]   # any language on test/emu devices
dp key back | home | enter | del | tab | esc | up | down …
dp swipe up|down|left|right [--on TARGET] [--dist 0.6]       # finger direction
dp scroll down|up [--on TARGET]  /  dp scroll --to text:物理    # content direction
dp draw line|wave|circle|check|cross|zigzag [in:TARGET | --box x,y,w,h] [--stylus] [--pressure 0.6]
dp draw "100,200 150,260 220,240"                            # points in shot space, "/" between strokes
dp zoom in|out [--on TARGET] [--factor 2]                     # two-finger pinch
dp wait text:保存しました [--timeout 10]   # also gone:…, activity:Name
dp launch [Activity|deeplink]   dp stop   dp reset (clears app data; emu/test only)
```

- Strokes go out as one real gesture each, with pressure (pen default 0.6). After `draw` / `zoom` dp prints how many pixels changed around the stroke: `画素が変わっていません` means nothing was drawn.
- Canvases are not in the UI tree, so the tree part usually says `変化なし`. Look at the pixel line, or `dp shot --region`.
- Note apps read some shapes as gestures. In many note apps a `zigzag` (and any back-and-forth scribble) is "scratch out": it erases the ink under it and leaves nothing. Use `wave` / `line` / `circle` / `check` for ordinary ink and `zigzag` only to test erasing. A stroke that starts off the page is ignored, so keep it inside the page with `--box`. In "stylus only" mode a finger stroke pans the page instead of drawing.

## Logs

- `dp log` covers the time since the last launch. It shows the crash (exception, the app's own frames, `Caused by`), ANRs and grouped errors (`E x3 Tag: message`), plus the step that ran just before a crash.
- `--since step12 | 60s | 5m | all` changes the time span. `--level W` adds warnings. `--grep RE` filters. `--full` writes everything to a file.
- Device noise (MIUI and graphics-driver lines the app's process prints) is dropped by `config/logs.toml`, and the head says how many lines were dropped. `--raw` shows them. If a new noise line keeps making `dp up` NG and it is clearly not the app's fault, propose adding a pattern there.
- `dp mark "メモ"` puts a marker into the journal and into logcat.

## Measuring

`dp perf` reads numbers about the app under test. It touches only the app's own package and never asks for confirmation, so use it instead of `dp raw shell dumpsys …` / `am start -W`.

```
dp perf start [--runs 3]   # cold start: stop, start, TotalTime; prints the median and each run
dp perf reset              # zero the frame counters
dp perf frames             # frames since the reset: total, slow frames (%), p50/p90/p95/p99, slow-frame causes
dp perf idle [--secs 10]   # reset, leave the screen alone, count frames: above 0 means something keeps drawing
dp perf mem                # TOTAL PSS, heaps, graphics, and Views / Activities counts (for leaks)
dp perf apk [--depth 2] [--top 8]   # size of the built APK and its largest groups; needs no running app
```

- A frame measurement is `dp perf reset`, then the actions (scroll, type, drag), then `dp perf frames`.
- Compare like with like: the same build type and device. A debug build is slower than what users get, and on a 120 Hz screen the frame budget is 8.3 ms.
- For a leak, open and close a screen several times and compare `dp perf mem` before and after: `Activities` and `ViewRootImpl` should come back to where they were.

## Dark theme, font size, rotation

On `emu` / `test` devices `dp env` changes how the device shows the app, and remembers the original values.

```
dp env show
dp env dark on|off        dp env font 1.3        dp env rotate land|port|auto
dp env reset              # back to what it was before the first change
```

Always finish with `dp env reset`. Look at the screen again after a change (`dp screen`, `dp shot`): the activity is usually recreated.

## Regression flows

Every action is numbered in the journal.

```
dp journal --last 20                          # see the step numbers
dp flow save note-add-page 31-36,38 --note "ページを足すと 2 ページ"
dp flow run note-add-page                     # or: dp flow run --all
```

- Flows live in `<app repo>/dpflows/*.dpflow`, one dp command per line. Index taps are already turned into `text:` / `id:` / `%` selectors.
- Add checks by editing the file:
  - `expect text:2/2`
  - `expect gone:text:読み込み中`
  - `expect activity:EditorActivity`
  - `expect enabled:text:やり直す` (also `disabled:` `checked:` `unchecked:` `selected:`)
  - `expect nocrash`
  - `expect nolog` (no E lines), `expect nolog:W`
- A failing run prints the line that failed, a screenshot and the tree.
- Flows never press outward buttons unless run with `dp flow run NAME --yes`, which needs the user's confirmation.

## Other

```
dp devices      dp doctor      dp setup (a person runs it once at the device)
dp raw shell getprop ro.build.version.release   # escape hatch; asks the user every time; safe devices: read-only
```

- `dp raw` stops and waits for a person each time. Reach for it only when no dp command gives the number; frames, memory, start time, APK size, dark theme, font size and rotation all have one.
- With two devices connected, pick one with `-s <serial>` (an emulator wins when there is one).
- The app is picked from the current repository; `-a NAME` (a name from `config/apps.toml`) overrides that.
- If the tree cannot be read ("画面が動き続けています"), use `dp shot` and look at the picture.

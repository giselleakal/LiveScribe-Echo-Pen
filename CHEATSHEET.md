# 🗒️ Echo Notebook — Cheat Sheet for Tomorrow Night

This is the only page you need. Follow it top to bottom. If you get stuck,
each step has a "you should see" note and a "trouble" fix at the bottom.

> 💡 **Golden rule:** every command in this project starts with `./run` and is
> typed in the **Linux terminal (WSL)**, in the project folder, like this:
>
> ```
> cd /home/giselle/echo
> ./run sync
> ```

---

## The 3-step nightly routine

### Step 1 — Turn the pen on and plug it in
- Use the **micro-USB data cable** (not a charge-only one — a charge cable won't work).
- Put the pen into the cradle / plug it in. **Pen screen should be on.**

### Step 2 — Attach the pen to Linux (this is the "magic" bridge step)
1. Open **PowerShell as Administrator** on the **Windows** side.
2. Run the "bridge" script, which lives in the project's `scripts` folder.
   In PowerShell, type `cd ` then drag-and-drop the `scripts` folder into the
   window (it fills in the path for you), press Enter, then run:
   ```
   .\windows-connect.ps1
   ```
   (If drag-and-drop doesn't work: the folder is
   `\\wsl.localhost\<distro>\home\giselle\echo\scripts` — navigate there in
   PowerShell, or just use whatever path worked for you tonight.)
3. Go back to the **Linux terminal** and check the pen is visible:
   ```
   lsusb
   ```
   **You should see:** a line with `1cfb:1032 Livescribe Echo(TM) Smartpen`
   > If you don't see it → re-run the PowerShell step, or try another cable.
   > This is the #1 "nothing works" cause — it's almost always the cable or
   > the bridge not being attached.

### Step 3 — Pull the notes off the pen and look at them
In the Linux terminal, in the project folder:
```
cd /home/giselle/echo
./run sync
```
- It may ask for a **sudo password** — that is **normal and expected**. Type
  your Linux password (nothing shows as you type — that's fine) and press Enter.
- **You should see:** it downloads pages/audio from the pen, then prints
  something like `rendered 10 pages ... 39 sessions`.
- This is the only "risky-looking" step; it just downloads your notes. It does
  **not** delete them from the pen.

Then, to see your notes as a website:
```
./run
```
- **You should see:** `Serving on http://127.0.0.1:8000` and the terminal will
  sit there (that's normal — it's "holding" the server).
- Open your browser and go to: **http://localhost:8000**
- To stop the server when done: press **Ctrl + C** in the terminal.

---

## Everyday commands (cheat reference)

| Command | What it does | When to use |
|---|---|---|
| `cd /home/giselle/echo` | Go to the project folder | Every time you open a terminal |
| `./run sync` | Download notes from pen + render | After plugging pen in |
| `./run` | Start the website (stays running) | To view notes |
| `./run convert` | Just re-render pages (no pen needed) | After editing data |
| `./run ocr` | Re-read handwriting into text | If pages lack transcription |
| `./run stt` | Transcribe the audio recordings | On-demand |
| `./run push` | Send notes to Hermes (my assistant) | When Hermes is set up |
| `Ctrl + C` | Stop whatever is running | To quit the server |
| `lsusb` | Check pen is attached | Step 2 check |
| `clear` | Clean the terminal screen | When it gets messy |

---

## Tomorrow night's plan (with the pen's new notes)

1. Plug in pen → PowerShell bridge → `lsusb` check (Steps 1–2).
2. `cd /home/giselle/echo` then `./run sync` → downloads the new notes.
3. `./run` → open **http://localhost:8000** → check the new pages appear.
4. Then we'll work on the **transcription training**: teaching Hermes to read
   your handwriting better by reviewing/correcting the text.

---

## When something goes wrong

| Problem | What to do |
|---|---|
| `lsusb` shows no pen | Re-run the PowerShell bridge step. Try another cable. Check pen is on. |
| `sudo` asks for password | Normal — type your Linux password, press Enter. |
| Says "pen not attached" | The bridge step didn't stick; redo PowerShell step. |
| Browser won't load localhost:8000 | Make sure `./run` is still running in the terminal. |
| Terminal "stuck"/no prompt | That's usually the server running — press **Ctrl + C** to get your prompt back. |
| Typed something wrong | Press the **↑ (up arrow)** to get the last command and fix it; **Tab** autocompletes. |
| Everything's confusing | You can always ask me (in this chat) — I'll tell you exactly what to type. |

---

## The 4 survival secrets of the terminal
1. **↑ up arrow** = bring back the last command (fix it and re-run — no retyping).
2. **Tab** = autocomplete. Type `./run s` then Tab → `./run sync`.
3. **Ctrl + C** = stop whatever is running / get your prompt back.
4. **You can't break the pen** — these commands only *read* notes off it
   (the one that deletes from the pen, `./run clean-pen`, is the ONLY one that
   erases, and it asks you "y/N" first — say **N** unless you really mean it).

If you ever get lost: type `./run help` — it lists every command.

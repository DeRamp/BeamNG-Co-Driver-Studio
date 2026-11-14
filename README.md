# BeamNG Co-Driver Studio

Record your own rally co-driver voice pack for BeamNG.drive. Windows, nothing to install.

## Get it
1. Download `CoDriverStudio-win64.zip` from [Releases](https://github.com/DeRamp/BeamNG-Co-Driver-Studio/releases).
2. Unzip anywhere and run `CoDriverStudio\CoDriverStudio.exe`.
   (Windows SmartScreen may warn about an unsigned app: **More info → Run anyway**.)

## Make a pack
1. **New voice pack** → type your co-driver's name.
2. **Record**: read the big line out loud.
   | Key | Action |
   |---|---|
   | Space | start / stop recording |
   | Enter | keep take, next phrase |
   | R | retake |
   | ← / → | previous / skip |

   Each take is auto-trimmed to just your words. If it's off, drag the orange handles.
   Tick **Hands-free** to stop automatically when you stop talking.
3. **Phrases**: see what's left (180 calls), listen back, add extra takes.
   All ticked takes of a call are picked at random in-game; untick one to leave it out.
4. **Effects**: pick a preset (Helmet Intercom, Rally Radio, Old CB, Walkie-Talkie) or tweak sliders.
   Your raw recordings are never changed.
5. **Sample Calls**: type e.g. `three left into hard right don't cut 100` and press Play.
6. **Pack & Export → Install to BeamNG**.

## Use it in-game
Restart BeamNG (or press Ctrl+L), start a rally mission and pick your co-driver from the
**Co-driver** dropdown on the start screen. Calls you haven't recorded stay silent.

## Build from source
```
python -m venv .venv
.venv\Scripts\pip install PySide6-Essentials numpy sounddevice soundfile pyinstaller pytest
.venv\Scripts\python -m codriver_studio   # run
.\build.ps1                                # test + build dist\CoDriverStudio-win64.zip
```
The phrase list comes from the game: `python tools\build_catalog.py` refreshes it after a BeamNG update.
Voice pack format notes: [ARCHITECTURE.md](ARCHITECTURE.md).

## License
MIT, see [LICENSE](LICENSE). Not affiliated with BeamNG GmbH.

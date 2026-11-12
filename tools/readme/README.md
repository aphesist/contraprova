# README images

`make_readme_svgs.py` draws the cascade and results images in the top-level README (`assets/readme/`), each
in a light and a dark version. The header and the card come from the design system's readme kit instead
(`tools/readme/readme.py` on aphesis's `design-system` branch; see its README):

```bash
python3 <aphesis-ds>/tools/readme/readme.py header assets/readme --icon contraprova --title contraprova \
  --gloss "probatio contraria" --line "cheap claude workers, with a control for every escalation." \
  --href https://contraprova.aphes.ist/
python3 <aphesis-ds>/tools/readme/readme.py card assets/readme --name card --title contraprova --gloss "probatio contraria" \
  --icon contraprova --line "on 88 fresh swe-bench pro tasks, on par with a single opus" \
  --line2 "at ~61% less cost per solved task." --number 61% --number-meta "less cost per solved task" \
  --href https://contraprova.aphes.ist/
```

The rest of this note is about `make_readme_svgs.py`. It is adapted from `tools/readme/` on the `main` branch of
[aphesist/aphesis](https://github.com/aphesist/aphesis), the aphes.ist design system's own README tools.

GitHub shows a README's SVG as an image: SMIL and CSS animations run, scripts and external files do not,
so the two faces are embedded as subsets of only the characters each image uses.

Inputs:
- `ui/vendor/lab-kit/v1/lab.css` and `fonts/`: the colour tokens and the two faces, from the vendored lab kit;
- `marks.json` (here): the brush icons, from aphesis `main` at `e536573` (`tools/readme/marks.json`, without
  the φ, which is the site's mark and not a project's);
- `eye_frames.json` (here): the orchestrator's eye, the design system's live `Aphesis.Eye` (owner's-mark preset)
  recorded tick by tick (125 ms) for 24 s with `record_eye.js`, so the README plays back its real blinks,
  saccades and tremble. To record it again from a checkout of aphesis's `design-system` branch:
  `node tools/readme/record_eye.js <aphesis>/system/components 192` (needs Playwright);
- `results/swepro-main/analysis.json`: every number shown.

The other icons move as `.aph-icon` does on the site: the stipple stirs, its two halves against each other, and
draws in toward the icon's heart (`.aph-gather`) when the token reaches it; the gate and the fork cut to their
second drawing while a block is shown.

```bash
pip install fonttools brotli numpy
python3 tools/readme/make_readme_svgs.py
```

Check several moments of each image before committing, not one screenshot: in a browser, pause the SVG
with `document.documentElement.pauseAnimations()` and step it with `setCurrentTime(seconds)`.

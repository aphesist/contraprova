# README images

`make_readme_svgs.py` draws the animated SVGs in the top-level README (`assets/readme/`), each in a light
and a dark version. It is adapted from `tools/readme/` on the `main` branch of
[aphesist/aphesis](https://github.com/aphesist/aphesis), the aphes.ist design system's own README tools.

GitHub shows a README's SVG as an image: SMIL and CSS animations run, scripts and external files do not,
so the two faces are embedded as subsets of only the characters each image uses.

Inputs:
- `ui/vendor/lab-kit/v1/lab.css` and `fonts/`: the colour tokens and the two faces, from the vendored lab kit;
- `marks.json` and `engine_frames.json` (here): the φ, the brush icons and the φ as the brush engine draws
  it, copied verbatim from aphesis `main` at `e536573` (`tools/readme/`);
- `results/swepro-main/analysis.json`: every number shown.

```bash
pip install fonttools brotli numpy
python3 tools/readme/make_readme_svgs.py
```

Check several moments of each image before committing, not one screenshot: in a browser, pause the SVG
with `document.documentElement.pauseAnimations()` and step it with `setCurrentTime(seconds)`.

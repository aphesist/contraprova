// Records the design system's live Eye (Aphesis.Eye, the owner's-mark preset) into eye_frames.json, so the README
// can play back its real blinks, saccades and tremble with SMIL. Needs Playwright and a checkout of aphesis's
// design-system branch:  node tools/readme/record_eye.js <aphesis>/system/components [ticks]
const fs = require('fs'), path = require('path');
const { chromium } = require('playwright');
const dir = path.resolve(process.argv[2]); const ticks = +(process.argv[3] || 200);
const r1 = (d) => d.replace(/-?\d+\.\d+/g, (n) => (+n).toFixed(1).replace(/\.0$/, ''));
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.setContent(`<!doctype html><style>${fs.readFileSync(path.join(dir, 'bundle.css'), 'utf8')}#e{width:56px}</style><div id="e"></div>`);
  await p.addScriptTag({ content: fs.readFileSync(path.join(dir, 'bundle.js'), 'utf8') });
  const seq = await p.evaluate(async (ticks) => {
    const el = document.getElementById('e');
    const h = Aphesis.Eye(el, { size: 56, mark: true, blink: true, gaze: 'auto', drawIn: false });
    const out = [];
    for (let i = 0; i < ticks; i++) {
      await new Promise((r) => setTimeout(r, 125));
      const svg = el.querySelector('svg');
      out.push({ state: h.state.blink + '/' + h.state.gaze, viewBox: svg.getAttribute('viewBox'),
        scale: svg.querySelector('feDisplacementMap').getAttribute('scale'),
        layers: [...svg.querySelectorAll('g > path')].map((q) => [q.getAttribute('class'), q.getAttribute('opacity') || '1', q.getAttribute('d')]) });
    }
    return out;
  }, ticks);
  await b.close();
  const key = (fr) => JSON.stringify(fr.layers), ids = new Map(), drawings = [];
  const sequence = seq.map((fr) => { const k = key(fr); if (!ids.has(k)) { ids.set(k, drawings.length); drawings.push(fr.layers.map(([c, o, d]) => [c, o, r1(d)])); } return ids.get(k); });
  const out = { source: 'Aphesis.Eye(el, {size: 56, mark: true, blink: true, gaze: "auto"}), one sample per 125 ms tick',
    tick: 0.125, viewBox: seq[0].viewBox, boil: seq[0].scale, states: seq.map((f) => f.state), sequence, drawings };
  fs.writeFileSync(path.join(__dirname, 'eye_frames.json'), JSON.stringify(out));
  console.log(`${seq.length} ticks, ${drawings.length} drawings, ${(JSON.stringify(out).length / 1024).toFixed(0)} KB`);
})();

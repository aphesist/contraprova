/* aphes.ist lab kit, lab.js: the optional script beside lab.css. https://aphes.ist/lab-kit/v1/
   Plain JavaScript, no dependencies, no framework. Everything here is optional: a page with lab.css alone is complete
   and readable. Loading this adds
     - the theme switch ([data-lab-toggle="theme"]) and the stipple ground ([data-lab-toggle="fog"]), both remembered
     - the stipple masks that chart areas and bands use (url(#lab-stipple-1..3)), and the class lab-js on <html>
     - tooltips for any element with data-tip inside .lab
     - the tokens as hex for canvas, WebGL and chart libraries: Lab.palette(), Lab.seq(), Lab.div(), Lab.color(name)
     - presets: Lab.plot() (Observable Plot), Lab.vega() (Vega and Vega-Lite config), Lab.points() (3D point clouds)
     - Lab.stipple(ctx, ...) to fill an area of a 2D canvas with cells, Lab.onTheme(fn) to redraw when the theme turns
     - the height of a sticky header or footer (.lab-header--sticky, .lab-footer--sticky) as --lab-header-h and
       --lab-footer-h on the root, for the receding bands and for links that land below the header
   The stipple ground is off by default and static when on; it drifts only with data-lab-fog="drift" on <body>, and
   never for someone whose system asks for reduced motion. */
(function () {
  'use strict';
  var VERSION = '1.1.0';
  var doc = document, root = doc.documentElement;
  var KEY_THEME = 'aphesis.lab.theme', KEY_FOG = 'aphesis.lab.fog';
  var mm = function (q) { return typeof matchMedia === 'function' ? matchMedia(q) : { matches: false, addEventListener: function () {} }; };
  var darkMq = mm('(prefers-color-scheme: dark)'), reduceMq = mm('(prefers-reduced-motion: reduce)');

  function store(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(k);
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } catch (e) { /* private window, blocked storage: the choice simply is not remembered */ }
    return null;
  }

  /* ---------- tokens read back from CSS, as hex ---------- */
  var probe = null, cache = {};
  function toHex(css) {
    if (!probe) { var c = doc.createElement('canvas'); c.width = c.height = 1; probe = c.getContext('2d', { willReadFrequently: true }); }
    probe.clearRect(0, 0, 1, 1); probe.fillStyle = '#000'; probe.fillStyle = css; probe.fillRect(0, 0, 1, 1);
    var d = probe.getImageData(0, 0, 1, 1).data;
    return '#' + [d[0], d[1], d[2]].map(function (x) { return x.toString(16).padStart(2, '0'); }).join('');
  }
  /* color('lab-cat-1') or color('--ink', el): a token's colour where el stands (default: the root), as '#rrggbb' */
  function color(name, el) {
    name = name.charAt(0) === '-' ? name : '--' + name;
    var key = name + (el ? '@' : '') + theme();
    if (!el && cache[key]) return cache[key];
    var v = getComputedStyle(el || root).getPropertyValue(name).trim();
    var hex = v ? toHex(v) : '#000000';
    if (!el) cache[key] = hex;
    return hex;
  }
  function range(prefix, n, el) { var out = []; for (var i = 1; i <= n; i++) out.push(color(prefix + i, el)); return out; }
  function palette(el) { return range('lab-cat-', 8, el); }
  function seq(el) { return range('lab-seq-', 7, el); }
  function div(el) { return range('lab-div-', 7, el); }
  function size(name, el) { return parseFloat(getComputedStyle(el || root).getPropertyValue('--' + name)) || 0; }

  /* ---------- theme ---------- */
  function theme() { var t = root.getAttribute('data-theme'); return t === 'dark' || t === 'light' ? t : (darkMq.matches ? 'dark' : 'light'); }
  var themeFns = [];
  function onTheme(fn) { themeFns.push(fn); return function () { themeFns = themeFns.filter(function (f) { return f !== fn; }); }; }
  function themeChanged() { cache = {}; sync(); themeFns.forEach(function (f) { try { f(theme()); } catch (e) { console.error(e); } }); }
  darkMq.addEventListener('change', function () { if (!root.hasAttribute('data-theme')) themeChanged(); });
  if (typeof MutationObserver === 'function') new MutationObserver(themeChanged).observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  /* setTheme('light' | 'dark' | 'auto'): 'auto' follows the system again */
  function setTheme(t) {
    if (t === 'light' || t === 'dark') { root.setAttribute('data-theme', t); store(KEY_THEME, t); }
    else { root.removeAttribute('data-theme'); store(KEY_THEME, null); }
  }
  var saved = store(KEY_THEME);
  if ((saved === 'light' || saved === 'dark') && !root.hasAttribute('data-theme')) root.setAttribute('data-theme', saved);

  /* ---------- the stipple ground: the site's fog, at a reader's density, cleared from the column of text ---------- */
  var NOISE = 'float h(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}' +
    'float n(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(h(i),h(i+vec2(1,0)),f.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),f.x),f.y);}' +
    'float fb(vec2 p){float s=0.,a=.5;for(int i=0;i<5;i++){s+=a*n(p);p=mat2(1.6,1.2,-1.2,1.6)*p;a*=.5;}return s;}' +
    'mat2 rot(float a){float c=cos(a),s=sin(a);return mat2(c,-s,s,c);}' +
    'float fog(vec2 uv,float T){float d=0.;for(int i=0;i<3;i++){float fi=float(i);vec2 q=rot(fi*2.39996)*uv*(1.6*pow(1.618,fi))+vec2(T*.013*pow(1.618,fi),-T*.008*pow(1.618,fi));' +
    'vec2 w=vec2(fb(q+vec2(0.,T*.01)),fb(q+vec2(5.2,1.3)));d+=fb(q+1.7*w)/(1.+fi*.6);}return d/1.9;}';
  var FS = 'precision highp float;uniform vec2 R;uniform float T,D,A,W;uniform vec3 F,B;' + NOISE +
    'void main(){vec2 cel=floor(gl_FragCoord.xy);vec2 uv=(cel-R*.5)/min(R.x,R.y);' +
    /* the clearing: the column of text (half-width W in uv) keeps only a sparse residue */
    'float ax=abs(uv.x)/W;float cl=1.-smoothstep(.75,1.25,ax);' +
    'float d=fog(vec2(uv.x*(1.-.6*exp(-ax*ax)),uv.y),T);float m=smoothstep(.44,.74,d);' +
    'float vin=smoothstep(1.45,.35,length(uv*vec2(.9,1.)))*.85+.15;' +
    'float dens=(m*.8+.012)*vin*D*(1.-.9*cl);' +
    'gl_FragColor=vec4(mix(B,F,step(h(cel),dens)*A),1.);}';
  var fogState = { on: false, cv: null, gl: null, U: null, t: 0, raf: 0, last: 0 };
  function rgb(hex) { return [1, 3, 5].map(function (i) { return parseInt(hex.substr(i, 2), 16) / 255; }); }
  function fogInit() {
    var cv = doc.createElement('canvas'); cv.className = 'lab-fog'; cv.setAttribute('aria-hidden', 'true');
    var gl = cv.getContext('webgl', { antialias: false, premultipliedAlpha: false, preserveDrawingBuffer: true });
    if (!gl) return false;
    function sh(t, s) { var o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); return o; }
    var pr = gl.createProgram();
    gl.attachShader(pr, sh(gl.VERTEX_SHADER, 'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}'));
    gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(pr); gl.useProgram(pr);
    var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var lp = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(lp); gl.vertexAttribPointer(lp, 2, gl.FLOAT, false, 0, 0);
    var U = {}; ['R', 'T', 'D', 'A', 'W', 'F', 'B'].forEach(function (k) { U[k] = gl.getUniformLocation(pr, k); });
    doc.body.insertBefore(cv, doc.body.firstChild);
    fogState.cv = cv; fogState.gl = gl; fogState.U = U; fogState.t = 37.5;   /* a fixed moment: the same ground on every visit */
    cv.addEventListener('webglcontextlost', function (e) { e.preventDefault(); });
    cv.addEventListener('webglcontextrestored', function () { fogState.cv = null; cv.remove(); if (fogState.on) { fogInit(); fogDraw(); } });
    return true;
  }
  function fogDraw() {
    var S = fogState, cv = S.cv, gl = S.gl, U = S.U; if (!cv) return;
    var cell = Math.max(1, Math.round(2 * (window.devicePixelRatio || 1))) / (window.devicePixelRatio || 1);   /* 2 CSS px, whole device px */
    var w = Math.max(1, Math.round(innerWidth / cell)), h = Math.max(1, Math.round(innerHeight / cell));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; gl.viewport(0, 0, w, h); }
    var page = doc.querySelector('.lab-page'), colW = page ? Math.min(page.getBoundingClientRect().width, innerWidth) : innerWidth * .7;
    gl.uniform2f(U.R, w, h); gl.uniform1f(U.T, S.t);
    gl.uniform1f(U.D, parseFloat(doc.body.getAttribute('data-lab-fog-density')) || .7);
    gl.uniform1f(U.A, size('lab-fog-alpha') || .55);
    gl.uniform1f(U.W, Math.max(.05, (colW / cell) * .5 / Math.min(w, h)));
    gl.uniform3fv(U.F, rgb(color('fog'))); gl.uniform3fv(U.B, rgb(color('bg')));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  function drifts() { return doc.body.getAttribute('data-lab-fog') === 'drift' && !reduceMq.matches; }
  function fogLoop(now) {
    var S = fogState; if (!S.on || !drifts()) { S.raf = 0; return; }
    if (doc.visibilityState === 'visible' && now - S.last > 100) {   /* ten frames a second is plenty for a slow drift */
      S.t += Math.min(.2, (now - S.last) / 1000) * .25; S.last = now; fogDraw();
    }
    S.raf = requestAnimationFrame(fogLoop);
  }
  /* fog(true | false): show or hide the ground; fog() says whether it is on */
  function fog(on) {
    if (on === undefined) return fogState.on;
    on = !!on;
    if (on && !fogState.cv && !fogInit()) on = false;
    fogState.on = on; store(KEY_FOG, on ? 'on' : 'off');
    root.classList.toggle('lab-fog-on', on);
    if (fogState.cv) fogState.cv.classList.toggle('on', on);
    if (on) { fogDraw(); if (drifts() && !fogState.raf) { fogState.last = performance.now(); fogState.raf = requestAnimationFrame(fogLoop); } }
    sync();
    return on;
  }
  addEventListener('resize', function () { if (fogState.on) fogDraw(); });
  onTheme(function () { if (fogState.on) fogDraw(); });
  reduceMq.addEventListener('change', function () { if (fogState.on) fog(true); });

  /* ---------- the stipple masks: the same 48px tiles as lab.css, as SVG masks for chart marks ---------- */
  var DENSITY = { 1: .16, 2: .32, 3: .56 }, TILE = 48, CELL = 2;
  function xorshift(seed) {
    var s = Math.imul(seed >>> 0, 2654435761) >>> 0 || 1;   /* spread a small seed over the word, then let it run in */
    for (var i = 0; i < 32; i++) { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; }
    return function () { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }
  function tilePath(density, seed) {
    var r = xorshift(seed), d = '';
    for (var y = 0; y < TILE; y += CELL) for (var x = 0; x < TILE; x += CELL) if (r() < density) d += 'M' + x + ' ' + y + 'h2v2h-2z';
    return d;
  }
  /* the masks are in user space, so a cell is 2 user units: draw charts at one unit per CSS pixel (as D3 and Plot do) */
  function defs() {
    if (doc.getElementById('lab-defs')) return;
    var s = '<svg id="lab-defs" aria-hidden="true" focusable="false" width="0" height="0" style="position:absolute;width:0;height:0;overflow:hidden"><defs>';
    for (var k = 1; k <= 3; k++) {
      s += '<pattern id="lab-stipple-tile-' + k + '" width="' + TILE + '" height="' + TILE + '" patternUnits="userSpaceOnUse">' +
        '<path fill="#fff" shape-rendering="crispEdges" d="' + tilePath(DENSITY[k], 1000 + k) + '"/></pattern>' +
        '<mask id="lab-stipple-' + k + '" maskUnits="userSpaceOnUse" x="-20000" y="-20000" width="40000" height="40000">' +
        '<rect x="-20000" y="-20000" width="40000" height="40000" fill="url(#lab-stipple-tile-' + k + ')"/></mask>';
    }
    s += '</defs></svg>';
    doc.body.insertAdjacentHTML('afterbegin', s);
    root.classList.add('lab-js');
  }
  /* defsMarkup(): the same masks as a string, to put inside an SVG that will leave the page (an export, a download) */
  function defsMarkup() { var el = doc.getElementById('lab-defs'); return el ? el.innerHTML : ''; }

  /* stipple(ctx, x, y, w, h, {density = .32, color, cell = 2, seed = 1}): light cells in a rectangle of a 2D canvas,
     on a grid anchored at the canvas origin. To fill a shape, clip to it first (ctx.save(); ctx.clip(path); ...; ctx.restore()). */
  function hash(ix, iy, seed) { var h = (ix * 374761393 + iy * 668265263 + seed * 2246822519) | 0; h = Math.imul(h ^ (h >>> 13), 1274126177); return ((h ^ (h >>> 16)) >>> 0) / 4294967296; }
  function stipple(ctx, x, y, w, h, o) {
    o = o || {}; var cell = o.cell || 2, dens = o.density === undefined ? .32 : o.density, seed = o.seed || 1;
    ctx.fillStyle = o.color || color('lab-mark');
    var x0 = Math.floor(x / cell), y0 = Math.floor(y / cell), x1 = Math.ceil((x + w) / cell), y1 = Math.ceil((y + h) / cell);
    for (var j = y0; j < y1; j++) for (var i = x0; i < x1; i++) if (hash(i, j, seed) < dens) ctx.fillRect(i * cell, j * cell, cell, cell);
  }

  /* ---------- tooltips: any element with data-tip inside .lab ---------- */
  var tip = null;
  function tips() {
    if (tip) return;
    tip = doc.createElement('div'); tip.className = 'lab-tip'; tip.setAttribute('role', 'tooltip'); doc.body.appendChild(tip);
    var cur = null;
    function place(x, y) {
      var r = tip.getBoundingClientRect(), px = x + 14, py = y + 14;
      if (px + r.width > innerWidth - 8) px = x - r.width - 14;
      if (py + r.height > innerHeight - 8) py = y - r.height - 14;
      tip.style.transform = 'translate(' + Math.max(8, px) + 'px,' + Math.max(8, py) + 'px)';
    }
    function show(el, x, y) { cur = el; tip.textContent = el.getAttribute('data-tip'); tip.classList.add('on'); place(x, y); }
    function hide() { cur = null; tip.classList.remove('on'); }
    doc.addEventListener('pointerover', function (e) { var el = e.target.closest && e.target.closest('.lab [data-tip]'); if (el) show(el, e.clientX, e.clientY); else if (cur) hide(); });
    doc.addEventListener('pointermove', function (e) { if (cur) place(e.clientX, e.clientY); });
    doc.addEventListener('focusin', function (e) { var el = e.target.closest && e.target.closest('.lab [data-tip]'); if (el) { var r = el.getBoundingClientRect(); show(el, r.right, r.top); } });
    doc.addEventListener('focusout', hide);
    addEventListener('scroll', hide, { passive: true });
  }

  /* ---------- controls: [data-lab-toggle="theme" | "fog"], their words say what pressing does ---------- */
  function sync() {
    var t = theme();
    doc.querySelectorAll('[data-lab-toggle="theme"]').forEach(function (b) {
      b.textContent = t === 'dark' ? 'open the eye' : 'close the eye';
      b.setAttribute('aria-label', t === 'dark' ? 'switch to the light theme' : 'switch to the dark theme');
    });
    doc.querySelectorAll('[data-lab-toggle="fog"]').forEach(function (b) {
      b.textContent = fogState.on ? 'fog · on' : 'fog · off';
      b.setAttribute('aria-pressed', fogState.on ? 'true' : 'false');
    });
  }
  doc.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-lab-toggle]'); if (!b) return;
    var k = b.getAttribute('data-lab-toggle');
    if (k === 'theme') setTheme(theme() === 'dark' ? 'light' : 'dark');
    if (k === 'fog') fog(!fogState.on);
  });

  /* ---------- presets for chart libraries and 3D ---------- */
  function fonts() { return { text: getComputedStyle(root).getPropertyValue('--font-text').trim(), display: getComputedStyle(root).getPropertyValue('--font-display').trim() }; }
  /* Observable Plot: Plot.plot({...Lab.plot(), marks: [...]}) */
  function plot() {
    return {
      style: { background: 'transparent', color: color('lab-axis-text'), fontFamily: fonts().text, fontSize: size('type-meta-size') + 'px', overflow: 'visible' },
      color: { range: palette(), unknown: color('lab-dim') },
    };
  }
  /* Vega and Vega-Lite: vegaEmbed(el, spec, {config: Lab.vega(), renderer: 'svg', actions: false}) */
  function vega() {
    var f = fonts(), axisText = color('lab-axis-text'), ink = color('ink'), meta = size('type-meta-size') || 11;
    return {
      background: null, font: f.text, padding: 4,
      view: { stroke: null },
      axis: { domainColor: color('lab-axis'), domainWidth: 1, tickColor: color('lab-axis'), tickWidth: 1, tickSize: 4,
        gridColor: color('lab-grid'), gridDash: [1, 3], gridWidth: 1, labelColor: axisText, labelFont: f.text, labelFontSize: meta,
        titleColor: axisText, titleFont: f.text, titleFontSize: meta, titleFontWeight: 'normal', labelPadding: 6, titlePadding: 10 },
      legend: { labelColor: axisText, labelFont: f.text, labelFontSize: size('type-label-size') || 13, titleColor: axisText, titleFont: f.text,
        titleFontSize: meta, titleFontWeight: 'normal', symbolType: 'square', symbolStrokeWidth: 0 },
      header: { labelColor: axisText, labelFont: f.text, titleColor: axisText, titleFont: f.text },
      title: { color: ink, font: f.display, fontSize: 28, fontWeight: 'normal', anchor: 'start', subtitleColor: axisText, subtitleFont: f.text },
      range: { category: palette(), ordinal: seq(), ramp: seq(), heatmap: seq(), diverging: div() },
      mark: { color: color('lab-mark') },
      line: { strokeWidth: size('lab-stroke') || 2 },
      point: { shape: 'square', filled: true, size: 36 },
      rule: { color: color('lab-ref') },
      text: { color: ink, font: f.text },
    };
  }
  /* 3D point clouds (three.js, regl, deck.gl...): square points of whole cells, sizes that do not attenuate, and the
     ground's colour as the depth fog, so far points sink into it instead of shrinking */
  function points() {
    var dpr = window.devicePixelRatio || 1;
    return {
      background: color('bg'), point: color('fog'), colors: palette(), pick: color('lab-ref'), dim: color('lab-dim'),
      size: size('lab-point') * dpr, emph: size('lab-point-emph') * dpr, pickSize: size('lab-point-pick') * dpr,
      dimAlpha: size('lab-dim-alpha') || .32, alpha: .9, square: true, sizeAttenuation: false, depthFog: { color: color('bg'), near: .6, far: 1 },
    };
  }

  /* ---------- sticky header and footer: their heights as --lab-header-h and --lab-footer-h, for the bands and for
     links that land below the header ---------- */
  function heights() {
    if (typeof ResizeObserver !== 'function') return;
    [['.lab-header--sticky', '--lab-header-h'], ['.lab-footer--sticky', '--lab-footer-h']].forEach(function (k) {
      var el = doc.querySelector(k[0]); if (!el) return;
      new ResizeObserver(function () { root.style.setProperty(k[1], Math.round(el.getBoundingClientRect().height) + 'px'); }).observe(el);
    });
  }

  /* ---------- start ---------- */
  function start() {
    defs(); tips(); sync(); heights();
    /* off unless the reader turned it on before, or the page asks to start with it (data-lab-fog-start) */
    var want = store(KEY_FOG);
    if (want === 'on' || (want === null && doc.body.hasAttribute('data-lab-fog-start'))) fog(true);
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start); else start();

  window.Lab = {
    version: VERSION, theme: theme, setTheme: setTheme, onTheme: onTheme, fog: fog,
    color: color, palette: palette, seq: seq, div: div, size: size,
    plot: plot, vega: vega, points: points, stipple: stipple, defsMarkup: defsMarkup,
  };
})();

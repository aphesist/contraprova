#!/usr/bin/env python3
"""
Animated SVGs for contraprova's README, in both themes (assets/readme/<name>-{light,dark}.svg).

They follow the aphes.ist design system, as the replay page does: a stippled fog for ground, the φ drawn by
hand, brush icons in rubedo, Aphesis Script (slanted) for names and glosses, Special Elite for text, and
the lab kit's categorical colours for the arms (A blue, B ochre, D green, as on the replay page). Nothing
is solid: areas are stipple, marks are brush.

GitHub shows a README's SVG as an image: SMIL and CSS animations run, scripts and external files do not.
So the two faces are embedded as subsets of only the characters each image uses, and everything is inline.

Every number is read from results/swepro-main/analysis.json, so the images cannot drift from the study.

Inputs, all in this repository (adapted from tools/readme on aphesis's main branch):
  ui/vendor/lab-kit/v1/lab.css     colour tokens, per theme
  ui/vendor/lab-kit/v1/fonts/      Aphesis Script (variable) and Special Elite
  tools/readme/marks.json          the φ and the brush icons
  tools/readme/engine_frames.json  the φ as the brush engine draws it, one outline per frame

    pip install fonttools brotli numpy
    python3 tools/readme/make_readme_svgs.py            # writes assets/readme/
"""
import argparse, base64, io, json, math, os, random, re

import numpy as np
from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
KIT = os.path.join(ROOT, 'ui', 'vendor', 'lab-kit', 'v1')
ap = argparse.ArgumentParser()
ap.add_argument('--css', default=os.path.join(KIT, 'lab.css'))
ap.add_argument('--aphesis', default=os.path.join(KIT, 'fonts', 'AphesisScript-VF.woff2'))
ap.add_argument('--elite', default=os.path.join(KIT, 'fonts', 'SpecialElite-latin.woff2'))
ap.add_argument('--marks', default=os.path.join(HERE, 'marks.json'))
ap.add_argument('--frames', default=os.path.join(HERE, 'engine_frames.json'))
ap.add_argument('--analysis', default=os.path.join(ROOT, 'results', 'swepro-main', 'analysis.json'))
ap.add_argument('--out', default=os.path.join(ROOT, 'assets', 'readme'))
ARGS = ap.parse_args()
os.makedirs(ARGS.out, exist_ok=True)


# ------------------------------------------------------------------ colours: the lab kit's tokens, resolved per theme
def oklch_hex(s):
    L, C, h = [float(x) for x in re.findall(r'[\d.]+', s)[:3]]
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l_, m_, s_ = L + .3963377774 * a + .2158037573 * b, L - .1055613458 * a - .0638541728 * b, L - .0894841775 * a - 1.2914855480 * b
    l, m, s3 = l_ ** 3, m_ ** 3, s_ ** 3
    rgb = [4.0767416621 * l - 3.3077115913 * m + .2309699292 * s3, -1.2684380046 * l + 2.6097574011 * m - .3413193965 * s3,
           -.0041960863 * l - .7034186147 * m + 1.7076147010 * s3]
    enc = lambda u: 12.92 * u if u <= .0031308 else 1.055 * u ** (1 / 2.4) - .055
    return '#' + ''.join('%02x' % round(max(0, min(1, enc(max(0, c)))) * 255) for c in rgb)


def themes():
    css = open(ARGS.css).read()
    blocks = re.findall(r'(:root,\[data-theme="light"\]|\[data-theme="dark"\])\{([^}]*)\}', css)
    decl = lambda body: dict(re.findall(r'--([\w-]+):([^;]+)', body))
    light = decl([b for sel, b in blocks if 'light' in sel][0])
    dark = dict(light, **decl([b for sel, b in blocks if 'dark' in sel][0]))

    def resolve(d):
        def r(v):
            m = re.fullmatch(r'var\(--([\w-]+)\)', v.strip())
            return r(d[m.group(1)]) if m else v.strip()
        return {k: oklch_hex(r(v)) for k, v in d.items() if r(v).startswith('oklch')}
    return {'light': resolve(light), 'dark': resolve(dark)}


THEMES = themes()

# ------------------------------------------------------------------ fonts
APH, ELITE = TTFont(ARGS.aphesis), TTFont(ARGS.elite)
ELITE_CMAP = ELITE.getBestCmap()


def font_face(path, family, text, variable=False):
    """woff2 subset of `path` holding only `text`, as an @font-face rule."""
    opts = ftsubset.Options(); opts.flavor = 'woff2'; opts.layout_features = ['*']; opts.notdef_outline = True
    opts.name_IDs = ['*']; opts.hinting = False
    font = TTFont(path)
    sub = ftsubset.Subsetter(opts); sub.populate(text=''.join(sorted(set(text + ' ')))); sub.subset(font)
    buf = io.BytesIO(); font.flavor = 'woff2'; font.save(buf)
    rng = 'font-weight:300 800;' if variable else ''
    return "@font-face{font-family:'%s';src:url(data:font/woff2;base64,%s) format('woff2');%s}" % (
        family, base64.b64encode(buf.getvalue()).decode(), rng)


def advance(text, size, spacing=0):
    """advance width of each character in Special Elite, for typing and alignment"""
    upm = ELITE['head'].unitsPerEm; hm = ELITE['hmtx']; out = []
    for c in text:
        g = ELITE_CMAP.get(ord(c)); w = hm[g][0] if g else upm * .5
        out.append(w / upm * size + spacing)
    return out


MARKS = json.load(open(ARGS.marks))
PHI = MARKS['phi']
A = json.load(open(ARGS.analysis))


def f(v): return ('%.2f' % v).rstrip('0').rstrip('.')
def esc(s): return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ------------------------------------------------------------------ stipple
def value_noise(w, h, scale, seed):
    rng = np.random.default_rng(seed)
    g = rng.random((int(h / scale) + 3, int(w / scale) + 3))
    ys, xs = np.mgrid[0:h, 0:w] / scale
    x0, y0 = xs.astype(int), ys.astype(int); fx, fy = xs - x0, ys - y0
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a, b, c, d = g[y0, x0], g[y0, x0 + 1], g[y0 + 1, x0], g[y0 + 1, x0 + 1]
    return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy


def fbm(w, h, scale, seed, octaves=4):
    s, a, tot = 0, 1, 0
    for o in range(octaves):
        s = s + a * value_noise(w, h, scale / 2 ** o, seed + o * 17); tot += a; a *= .5
    return s / tot


def smooth(e0, e1, x): t = np.clip((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t)


def squares(dens, cell, seed, ox=0, oy=0):
    """stipple path: a square cell wherever density beats a fixed per-cell threshold"""
    rng = np.random.default_rng(seed); h, w = dens.shape
    yy, xx = np.mgrid[0:h, 0:w]
    ign = np.mod(52.9829189 * np.mod(xx * .06711056 + yy * .00583715, 1), 1)
    thr = .65 * ign + .35 * rng.random((h, w))
    ys, xs = np.nonzero(dens > thr)
    s = max(1, round(cell * .78))
    return ''.join('M%d %dh%dv%dh-%dz' % (round(ox + x * cell), round(oy + y * cell), s, s, s) for y, x in zip(ys, xs))


def fog_layers(W, H, cell, seed, calm=(), amount=1.0, n=3):
    """n fog layers (one field, drifted) that cross-fade so the fog seems to move. calm: ellipses (cx, cy, rx, ry)."""
    w, h = W // cell + 1, H // cell + 1
    ys, xs = np.mgrid[0:h, 0:w] * cell
    mask = np.ones((h, w))
    for cx, cy, rx, ry in calm:
        q = np.sqrt(((xs - cx) / rx) ** 2 + ((ys - cy) / ry) ** 2)
        mask *= 1 - .9 * (1 - smooth(.55, 1.05, q))
    big = fbm(w + 40, h + 20, 22, seed)
    out = []
    for i in range(n):
        d = big[i * 5: i * 5 + h, i * 9: i * 9 + w]
        out.append(squares((smooth(.46, .74, d) * .55 + .015) * mask * amount, cell, seed + i))
    return out


def fog_svg(paths, color, dur=18, opacity=.5):
    n = len(paths); out = []
    for i, p in enumerate(paths):
        kt = ';'.join(f(k / n) for k in range(n + 1))
        v = ';'.join('1' if (k % n) == i else '0' for k in range(n + 1))
        out.append('<path d="%s" fill="%s" opacity="%s"><animate attributeName="fill-opacity" dur="%ss" repeatCount="indefinite" '
                   'calcMode="spline" keyTimes="%s" values="%s" keySplines="%s"/></path>' % (p, color, opacity, dur, kt, v, ';'.join(['.45 0 .55 1'] * n)))
    return ''.join(out)


def bezier(p0, p1, p2, p3, n=80):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3))
            for t in (i / n for i in range(n + 1))]


def brush_line(pts, seed, cell=3, width=2.4):
    """a line laid as stipple: square cells along the path, thicker in the middle, ragged at both ends"""
    rng = random.Random(seed); out = []; acc = 0.0
    seg = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(math.dist(a, b) for a, b in seg) or 1
    s = cell
    for (x0, y0), (x1, y1) in seg:
        L = math.dist((x0, y0), (x1, y1))
        if not L: continue
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        t = 0.0
        while t < L:
            u = (acc + t) / total
            wv = width * (.35 + .65 * math.sin(math.pi * min(1, max(0, u))) ** .6)
            k = max(1, round(wv))
            for j in range(k):
                if rng.random() < .82:
                    off = (j - (k - 1) / 2) * s * .9 + rng.uniform(-.6, .6)
                    x = x0 + (x1 - x0) * t / L + nx * off; y = y0 + (y1 - y0) * t / L + ny * off
                    out.append('M%s %sh%dv%dh-%dz' % (f(x - s / 2), f(y - s / 2), s - 1, s - 1, s - 1))
            t += s * 1.15
        acc += L
    return ''.join(out)


def blob(r, seed, cell=3):
    """a round cluster of stipple cells centred on 0,0: the token that travels the cascade"""
    n = int(2 * r / cell) + 1
    ys, xs = (np.mgrid[0:n, 0:n] * cell - r)
    dens = smooth(1.0, .15, np.sqrt(xs ** 2 + ys ** 2) / r) * 1.1
    return squares(dens, cell, seed, -r, -r)


def stipple_rect(w, h, seed, cell=4, level=.62, edge=True):
    gw, gh = int(w // cell) + 1, int(h // cell) + 1
    d = np.full((gh, gw), level)
    if edge:
        n = fbm(gw + 4, gh + 4, 6, seed)[:gh, :gw]
        d = d * (.75 + .5 * n)
    return squares(d, cell, seed)


# ------------------------------------------------------------------ pieces
ENGINE = None


def phi_drawn(x, y, h, color, dur=14, start=.6, fid='boilE'):
    """the φ as the brush engine draws it on aphes.ist, played back frame by frame (engine_frames.json)"""
    global ENGINE
    if ENGINE is None: ENGINE = json.load(open(ARGS.frames))
    fr = [p or 'M%d %dl0 0z' % (ENGINE['bbox'][0] + 326, ENGINE['bbox'][3]) for p in ENGINE['frames']]
    n = len(fr) - 1; draw = ENGINE['dur']
    x0, y0, x1, y1 = ENGINE['bbox']; s = h / (y1 - y0); tx, ty = x - x0 * s, y - y0 * s
    kt = [0] + [(start + i / n * draw) / dur for i in range(n + 1)]
    filt = ('<filter id="%s" x="-5%%" y="-5%%" width="110%%" height="110%%"><feTurbulence type="fractalNoise" baseFrequency=".03" numOctaves="1" seed="2">'
            '<animate attributeName="seed" dur="1s" repeatCount="indefinite" calcMode="discrete" values="2;7;13;21;34;55;89;144"/></feTurbulence>'
            '<feDisplacementMap in="SourceGraphic" scale="7" xChannelSelector="R" yChannelSelector="G"/></filter>' % fid)
    return ('<defs>%s</defs><g transform="translate(%s %s) scale(%s)">'
            '<animate attributeName="opacity" dur="%ss" repeatCount="indefinite" keyTimes="0;.86;.93;1" values="1;1;0;0"/>'
            '<path fill="%s" filter="url(#%s)" d="%s"><animate attributeName="d" dur="%ss" repeatCount="indefinite" calcMode="discrete" keyTimes="%s" values="%s"/></path></g>'
            % (filt, f(tx), f(ty), f(s), dur, color, fid, fr[0], dur, ';'.join(f(k) for k in kt), ';'.join([fr[0]] + fr)))


def phi_static(x, y, h, color):
    vb = [float(v) for v in PHI['viewBox'].split()]
    s = h / vb[3]
    return '<g transform="translate(%s %s) scale(%s)"><path d="%s" fill="%s" fill-rule="evenodd"/></g>' % (
        f(x - vb[0] * s), f(y - vb[1] * s), f(s), PHI['d'], color)


def icon(name, x, y, size, color):
    g = MARKS[name]; s = size / 24
    dots = '<path d="%s" fill="%s" opacity=".85"/>' % (g['dots'], color) if g['dots'] else ''
    return '<g transform="translate(%s %s) scale(%s)"><path d="%s" fill="%s"/>%s</g>' % (f(x), f(y), f(s), g['strokes'], color, dots)


def window(dur, t0, t1, fade=.25, attr='opacity'):
    """an animate that shows an element from t0 to t1 (seconds) in a loop of dur"""
    k = [0, max(0, t0 - fade), t0, t1, min(dur, t1 + fade), dur]
    k = [k[0]] + [max(k[i], k[i - 1] + .001) for i in range(1, len(k))]
    return '<animate attributeName="%s" dur="%ss" repeatCount="indefinite" keyTimes="%s" values="0;0;1;1;0;0"/>' % (
        attr, dur, ';'.join(f(min(1, v / dur)) for v in k))


def typed(text, x, y, size, color, dur, start, hold, cls='elite halo-s', anchor='start', spacing=0, erase=True):
    """types `text` one character at a time with an uneven rhythm, holds, then erases word by word"""
    adv = advance(text, size, spacing=spacing); total = sum(adv)
    if anchor == 'end': x -= total
    if anchor == 'middle': x -= total / 2
    rng = random.Random(len(text) * 7 + int(size)); t = start; times = []
    for i, c in enumerate(text):
        t += .05 * math.exp(rng.gauss(0, .32)) * (1.4 if (i and text[i - 1] == ' ') else 1); times.append(t)
    end = t + hold; out = []; cx = x
    words = []; wi = 0
    for c in text:
        if c == ' ': wi += 1
        words.append(wi)
    nw = max(words) + 1
    for i, c in enumerate(text):
        if c != ' ':
            gone = end + (nw - 1 - words[i]) * .12 if erase else dur - .01
            kt = [0, times[i] / dur, min(.999, times[i] / dur + .0005), min(.9995, gone / dur), min(.9999, gone / dur + .0005), 1]
            kt = [kt[0]] + [max(kt[j], kt[j - 1] + 1e-4) for j in range(1, len(kt))]
            out.append('<text x="%s" y="%s" class="%s" font-size="%s" fill="%s" opacity="0">%s<animate attributeName="opacity" dur="%ss" '
                       'repeatCount="indefinite" keyTimes="%s" values="0;0;1;1;0;0"/></text>'
                       % (f(cx), f(y), cls, f(size), color, esc(c), dur, ';'.join(f(min(1, k)) for k in kt)))
        cx += adv[i]
    caret_x = [f(x + sum(adv[:i + 1])) for i in range(len(text))]
    on, off = max(0, start - .25) / dur, min(.999, (end + nw * .12 + .3) / dur)
    out.append('<rect y="%s" width="%s" height="%s" fill="%s" opacity=".75" x="%s" visibility="hidden">'
               '<animate attributeName="x" dur="%ss" repeatCount="indefinite" calcMode="discrete" keyTimes="0;%s;1" values="%s;%s;%s"/>'
               '<animate attributeName="visibility" dur="%ss" repeatCount="indefinite" calcMode="discrete" keyTimes="0;%s;%s" values="hidden;visible;hidden"/>'
               '<animate attributeName="opacity" dur="1.06s" repeatCount="indefinite" calcMode="discrete" values=".75;0"/></rect>'
               % (f(y - size * .78), f(size * .55), f(size * .95), color, f(x), dur, ';'.join(f(tt / dur) for tt in times), f(x),
                  ';'.join(caret_x), caret_x[-1], dur, f(on), f(off)))
    return ''.join(out)


def txt(s, x, y, size, color, cls='elite', anchor='start', extra=''):
    return '<text x="%s" y="%s" class="%s" font-size="%s" fill="%s" text-anchor="%s"%s>%s</text>' % (
        f(x), f(y), cls, f(size), color, anchor, extra, esc(s))


def svg(name, W, H, body_fn, aph_text='', elite_text='', title=''):
    for tid, T in THEMES.items():
        css = []
        if aph_text: css.append(font_face(ARGS.aphesis, 'Aphesis Script', aph_text, variable=True))
        if elite_text: css.append(font_face(ARGS.elite, 'Special Elite', elite_text))
        css.append(".aph{font-family:'Aphesis Script',serif;font-variation-settings:'wght' 400,'slnt' -12}"
                   ".elite{font-family:'Special Elite',monospace}"
                   ".halo{paint-order:stroke;stroke:%s;stroke-width:7px;stroke-linejoin:round;stroke-opacity:.92}"
                   ".halo-s{paint-order:stroke;stroke:%s;stroke-width:3px;stroke-linejoin:round}" % (T['bg'], T['bg']))
        doc = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img"><title>%s</title>'
               '<style>%s</style><rect width="%d" height="%d" fill="%s"/>%s</svg>'
               % (W, H, W, H, esc(title), ''.join(css), W, H, T['bg'], body_fn(T, tid)))
        p = os.path.join(ARGS.out, '%s-%s.svg' % (name, tid))
        open(p, 'w').write(doc); print('%-26s %6.1f KB' % (os.path.basename(p), len(doc) / 1024))


# ------------------------------------------------------------------ numbers
S = A['solve']; P = A['primary']; CO = A['co_primary']; EV = A['escalation_value']; C = A['cost']
pct = lambda v: '%.1f%%' % (100 * v)
usd = lambda v: '$%.3f' % v
FORKS = EV['n_forks']; NP = A['n_pairs']


# ------------------------------------------------------------------ 1. hero: the seal drawn in the fog, the name, the claim typed
def hero():
    W, H = 1200, 460
    calm = [(230, 230, 210, 240), (760, 220, 400, 150), (930, 430, 300, 50)]
    layers = fog_layers(W, H, 4, 23, calm)
    gloss = 'probatio contraria'
    name = 'contraprova'
    tag = 'escalation is a claim. contraprova is the control.'
    line = '>> fork: haiku again %d/%d, opus %d/%d. the gap is what escalation buys.' % (
        EV['control_haiku_noise_floor']['solved'], FORKS, EV['treatment_opus']['solved'], FORKS)

    def body(T, tid):
        return (fog_svg(layers, T['fog']) + phi_drawn(120, 50, 360, T['seal']) +
                txt(gloss, 476, 132, 24, T['ink-soft'], 'aph halo') +
                txt(name, 466, 252, 128, T['ink'], 'aph halo') +
                txt(tag, 476, 310, 20, T['ink-soft'], 'elite halo', extra=' letter-spacing="1"') +
                typed(line, 1176, 438, 13, T['accent-text'], 14, 3.0, 6.0, anchor='end', spacing=1.2) +
                icon('eye0' if tid == 'light' else 'eye1', 22, 414, 28, T['accent']))
    svg('hero', W, H, body, gloss + name, tag + line, 'contraprova: escalation is a claim, contraprova is the control')


# ------------------------------------------------------------------ 2. the cascade: a brief travels, the gate passes it or forks it
def cascade():
    W, H, dur = 1200, 470, 16
    O, Wk, G = (110, 225), (390, 225), (640, 225)
    Pp, F, Cc, Tt = (930, 95), (790, 345), (960, 290), (960, 410)
    e_ow = [(O[0] + 46, O[1]), (Wk[0] - 46, Wk[1])]
    e_wg = [(Wk[0] + 46, Wk[1]), (G[0] - 46, G[1])]
    e_gp = bezier((G[0] + 40, G[1] - 14), (740, 170), (790, Pp[1]), (Pp[0] - 44, Pp[1]))
    e_gf = bezier((G[0] + 34, G[1] + 26), (700, 300), (730, F[1]), (F[0] - 40, F[1]))
    e_fc = bezier((F[0] + 34, F[1] - 12), (860, 310), (890, Cc[1]), (Cc[0] - 44, Cc[1]))
    e_ft = bezier((F[0] + 34, F[1] + 12), (860, 385), (890, Tt[1]), (Tt[0] - 44, Tt[1]))
    d = lambda pts: 'M' + ' L'.join('%s %s' % (f(x), f(y)) for x, y in pts)
    fogl = fog_layers(W, H, 4, 41, [(600, 235, 640, 260)], amount=.8)
    tok = blob(12, 5)

    def pulse(pts, t0, t1, color):
        return ('<g opacity="0">%s<path d="%s" fill="%s"/><animateMotion dur="%ss" repeatCount="indefinite" path="%s" '
                'keyPoints="0;0;1;1" keyTimes="0;%s;%s;1" calcMode="linear"/></g>'
                % (window(dur, t0, t1, .12), tok, color, dur, d(pts), f(t0 / dur), f(t1 / dur)))

    def node(c, ic, name, sub, T, below=True, anchor='middle', ic2=None, swap=None):
        x, y = c
        disc = '<circle cx="%s" cy="%s" r="30" fill="%s"/>' % (f(x), f(y), T['bg'])
        icn = icon(ic, x - 22, y - 22, 44, T['accent'])
        if ic2:   # the gate turns to its second state while a block is shown
            a, b = swap
            flip = lambda v: ('<animate attributeName="opacity" dur="%ss" repeatCount="indefinite" calcMode="discrete" '
                              'keyTimes="0;%s;%s" values="%s"/>' % (dur, f(a / dur), f(b / dur), v))
            icn = '<g>%s%s</g><g opacity="0">%s%s</g>' % (icn, flip('1;0;1'), icon(ic2, x - 22, y - 22, 44, T['accent']), flip('0;1;0'))
        ty = y + 60 if below else y - 44
        tx = x if anchor == 'middle' else x + 40
        ty2 = ty + 22
        if anchor != 'middle': ty, ty2 = y + 2, y + 24
        return (disc + icn + txt(name, tx, ty, 30, T['ink'], 'aph halo', anchor) +
                txt(sub, tx, ty2, 13, T['ink-soft'], 'elite halo-s', anchor, ' letter-spacing=".6"'))

    ctrl, treat = EV['control_haiku_noise_floor'], EV['treatment_opus']
    t = {'ow': (.6, 2.0), 'wg': (2.0, 3.0), 'gp': (3.0, 4.4), 'ow2': (6.6, 8.0), 'wg2': (8.0, 9.0), 'gf': (9.0, 10.2), 'split': (10.2, 11.8)}
    note = '>> %d of %d tasks forked. escalation value %+d points [%+.1f, %+.1f]' % (
        FORKS, NP, round(EV['EV_points']), EV['ci95_points'][0], EV['ci95_points'][1])

    def body(T, tid):
        line = T['line'] if 'line' in T else T['n-300']
        edges = ''.join('<path d="%s" fill="%s"/>' % (brush_line(p, i), T['ink-soft'])
                        for i, p in enumerate([e_ow, e_wg, e_gp, e_gf, e_fc, e_ft]))
        brief = typed('scope: test: goal:', (O[0] + Wk[0]) / 2, O[1] - 16, 12, T['ink-soft'], dur, .3, 1.6, anchor='middle', spacing=.6)
        brief2 = typed('scope: test: goal:', (O[0] + Wk[0]) / 2, O[1] - 16, 12, T['ink-soft'], dur, 6.3, 1.6, anchor='middle', spacing=.6)
        verdict_pass = '<g opacity="0">%s%s</g>' % (txt('pass', G[0], G[1] - 44, 14, T['lab-pass'], 'elite halo-s', 'middle', ' letter-spacing="2"'), window(dur, 3.0, 5.6))
        verdict_block = '<g opacity="0">%s%s</g>' % (txt('block', G[0], G[1] - 44, 14, T['accent-text'], 'elite halo-s', 'middle', ' letter-spacing="2"'), window(dur, 9.0, 12.8))
        return (fog_svg(fogl, T['fog'], opacity=.4) + edges + brief + brief2 + verdict_pass + verdict_block +
                pulse(e_ow, *t['ow'], T['lab-cat-3']) + pulse(e_wg, *t['wg'], T['lab-cat-3']) + pulse(e_gp, *t['gp'], T['lab-cat-3']) +
                pulse(e_ow, *t['ow2'], T['lab-cat-3']) + pulse(e_wg, *t['wg2'], T['lab-cat-3']) + pulse(e_gf, *t['gf'], T['accent']) +
                pulse(e_fc, *t['split'], T['lab-cat-2']) + pulse(e_ft, *t['split'], T['lab-cat-1']) +
                node(O, 'eye1' if tid == 'dark' else 'eye0', 'orchestrator', 'opus · plans from outlines', T) +
                node(Wk, 'luna', 'worker', 'haiku · does the job', T) +
                node(G, 'thr0', 'gate', 'scope · edits · tests', T, ic2='thr1', swap=(9.0, 12.8)) +
                node(Pp, 'sol', 'shipped', '%d of %d passed on haiku' % (NP - FORKS, NP), T, anchor='start') +
                node(F, 'term0', 'fork', 'from the same logged point', T, below=True) +
                node(Cc, 'luna', 'contraprova', 'haiku again: %d of %d' % (ctrl['solved'], ctrl['n']), T, anchor='start') +
                node(Tt, 'sol', 'escalation', 'opus: %d of %d' % (treat['solved'], treat['n']), T, anchor='start') +
                typed(note, 40, 450, 13, T['accent-text'], dur, 11.2, 3.6, spacing=1.0))
    svg('cascade', W, H, body, 'orchestrator worker gate shipped fork contraprova escalation',
        'scope: test: goal: opus · plans from outlines haiku · does the job scope · edits · tests passed on haiku from the same logged point '
        'again: of opus: pass block ' + note + '0123456789',
        'the governed cascade: an orchestrator briefs a haiku worker, a gate passes the result or forks it into a haiku retry and an opus escalation')


# ------------------------------------------------------------------ 3. results: solve rates with wilson intervals, the margin, cost per solved
def results():
    W, H, dur = 1200, 470, 14
    arms = [('A', 'single opus', 'arm a', 'lab-cat-1'), ('D', 'the cascade', 'arm d', 'lab-cat-3'),
            ('B_derived', 'its first haiku try', 'arm b', 'lab-cat-2')]
    x0, x1, lo, hi = 330, 620, .70, 1.0
    X = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    rows = [120, 190, 260]
    bars = {k: stipple_rect(X(S[k]['rate']) - x0, 26, 7 + i, 4) for i, (k, *_r) in enumerate(arms)}
    cx0, cx1, cmax = 870, 1110, .15
    CX = lambda v: cx0 + v / cmax * (cx1 - cx0)
    cost = [('A', 'single opus', 'lab-cat-1', 120), ('D', 'the cascade', 'lab-cat-3', 190)]
    cbars = {k: stipple_rect(CX(C['per_solved'][k]) - cx0, 26, 31 + i, 4) for i, (k, *_r) in enumerate(cost)}
    fogl = fog_layers(W, H, 4, 57, [(340, 210, 400, 230), (920, 180, 330, 170), (600, 410, 620, 70)], amount=.7)
    # difference scale: d - a in points, with the pre-registered margin
    dx0, dx1, dlo, dhi = 330, 620, -12, 6
    DX = lambda v: dx0 + (v - dlo) / (dhi - dlo) * (dx1 - dx0)
    dy = 355
    ci = P['ci95_points']

    def grow(i, w, t0):
        a, b = t0 + .25 * i, t0 + .25 * i + 1.1
        return ('<animate attributeName="width" dur="%ss" repeatCount="indefinite" calcMode="spline" keyTimes="0;%s;%s;.93;.97;1" '
                'values="0;0;%s;%s;0;0" keySplines="0 0 1 1;.3 0 .2 1;0 0 1 1;.4 0 .6 1;0 0 1 1"/>' % (dur, f(a / dur), f(b / dur), f(w), f(w)))

    def body(T, tid):
        o = [fog_svg(fogl, T['fog'], opacity=.35)]
        o.append(txt('solve rate, %d fresh swe-bench pro tasks' % NP, 60, 58, 14, T['ink-soft'], 'elite halo-s', extra=' letter-spacing="1.5"'))
        o.append(txt('cost per solved task', 870, 58, 14, T['ink-soft'], 'elite halo-s', extra=' letter-spacing="1.5"'))
        for gx in (.7, .8, .9, 1.0):   # grid
            o.append('<path d="%s" fill="%s" opacity=".7"/>' % (brush_line([(X(gx), 92), (X(gx), 296)], int(gx * 100), 2, 1), T['n-500']))
            o.append(txt('%d%%' % round(gx * 100), X(gx), 312, 11, T['ink-soft'], 'elite', 'middle'))
        for i, (k, label, code, col) in enumerate(arms):
            y = rows[i]; s = S[k]; w = X(s['rate']) - x0
            o.append(txt(label, 60, y + 14, 26, T['ink'], 'aph halo'))
            o.append(txt('%s · %d/%d' % (code, s['solved'], s['n']), 60, y + 33, 12, T['ink-soft'], 'elite halo-s', extra=' letter-spacing=".8"'))
            o.append('<clipPath id="cb%d"><rect x="%s" y="%s" width="0" height="30">%s</rect></clipPath>' % (i, x0, y - 2, grow(i, w + 2, .4)))
            o.append('<g clip-path="url(#cb%d)"><path transform="translate(%s %s)" d="%s" fill="%s"/></g>' % (i, x0, y, bars[k], T[col]))
            wl, wh = s['wilson95']
            o.append('<g opacity="0">%s<path d="%s" fill="%s"/>%s</g>' % (
                window(dur, 1.9 + .25 * i, dur * .93, .3),
                brush_line([(X(max(lo, wl)), y + 13), (X(wh), y + 13)], 90 + i, 2, 2) +
                brush_line([(X(max(lo, wl)), y + 5), (X(max(lo, wl)), y + 21)], 95 + i, 2, 1) +
                brush_line([(X(wh), y + 5), (X(wh), y + 21)], 99 + i, 2, 1), T['ink'],
                txt(pct(s['rate']), X(wh) + 12, y + 20, 18, T['ink'], 'elite halo-s')))
        # margin scale
        o.append(txt('cascade minus opus, points', 60, dy + 6, 12, T['ink-soft'], 'elite halo-s', extra=' letter-spacing=".8"'))
        o.append('<path d="%s" fill="%s"/>' % (brush_line([(dx0, dy), (dx1, dy)], 7, 2, 1), T['ink-soft']))
        for v in (-10, -5, 0, 5):
            o.append(txt('%+d' % v if v else '0', DX(v), dy + 26, 11, T['ink-soft'], 'elite', 'middle'))
        o.append('<path d="%s" fill="%s"/>' % (brush_line([(DX(P['margin_points']), dy - 18), (DX(P['margin_points']), dy + 10)], 8, 2, 2), T['accent']))
        o.append(txt('margin', DX(P['margin_points']), dy - 24, 11, T['accent-text'], 'elite halo-s', 'middle'))
        o.append('<g opacity="0">%s<path d="%s" fill="%s"/>%s%s</g>' % (
            window(dur, 3.2, dur * .93, .3),
            brush_line([(DX(ci[0]), dy - 6), (DX(ci[1]), dy - 6)], 12, 3, 3), T['lab-cat-3'],
            '<rect x="%s" y="%s" width="7" height="7" fill="%s"/>' % (f(DX(P['diff_D_minus_A_points']) - 3.5), dy - 10, T['ink']),
            txt('%+.1f [%+.1f, %+.1f], non-inferior' % (P['diff_D_minus_A_points'], ci[0], ci[1]), dx1 + 14, dy - 1, 13, T['ink'], 'elite halo-s')))
        # cost
        for gx in (0, .05, .1, .15):
            o.append(txt('$%.2f' % gx, CX(gx), 240, 11, T['ink-soft'], 'elite', 'middle'))
        for i, (k, label, col, y) in enumerate(cost):
            w = CX(C['per_solved'][k]) - cx0
            o.append(txt(label, 690, y + 20, 22, T['ink'], 'aph halo'))
            o.append('<clipPath id="cc%d"><rect x="%s" y="%s" width="0" height="30">%s</rect></clipPath>' % (i, cx0, y - 2, grow(i, w + 2, .9)))
            o.append('<g clip-path="url(#cc%d)"><path transform="translate(%s %s)" d="%s" fill="%s"/></g>' % (i, cx0, y, cbars[k], T[col]))
            o.append('<g opacity="0">%s%s</g>' % (window(dur, 2.3 + .25 * i, dur * .93, .3),
                                                    txt(usd(C['per_solved'][k]), CX(C['per_solved'][k]) + 10, y + 20, 18, T['ink'], 'elite halo-s')))
        r = CO['ci95']
        o.append('<g opacity="0">%s%s%s</g>' % (window(dur, 3.0, dur * .93, .3),
                                                  txt('%.2f× [%.2f, %.2f]' % (CO['cost_per_solved_ratio_D_over_A'], r[0], r[1]), 690, 290, 30, T['accent-text'], 'elite halo'),
                                                  txt('the cascade over single opus, per solved task', 690, 314, 12, T['ink-soft'], 'elite halo-s', extra=' letter-spacing=".8"')))
        o.append(typed('>> pre-registered. exact mcnemar p = %.3f. spend $%.2f.' % (P['mcnemar_p'], C['total']['all_recorded']),
                       1150, 440, 13, T['accent-text'], dur, 4.0, 7.5, anchor='end', spacing=1.0))
        return ''.join(o)
    svg('results', W, H, body, 'single opus the cascade its first haiku try',
        'solve rate, fresh swe-bench pro tasks cost per solved task arm a d b cascade minus opus, points margin non-inferior '
        'the cascade over single opus, per solved task >> pre-registered. exact mcnemar p = spend $.%×[]+-,/0123456789',
        'solve rate with 95% wilson intervals for single opus, the cascade and its first haiku try, the non-inferiority margin, and cost per solved task')


if __name__ == '__main__':
    hero(); cascade(); results()

/* Pit lane on the minimap: readability helpers (pure functions, also run by the tests with node).
 *
 * separation(): where the reconstructed pit lane runs so close to the main track that the two
 *   roads would merge on the small map, it is pushed away from the track *on the side where it
 *   really is*, by just enough screen pixels to show a gap (capped). Where they are apart
 *   already nothing moves, and the shift fades out towards the pit entry / exit so the lane
 *   still branches off and rejoins the track where it really does. The shape is not changed.
 * carOnPit(): a car in the pit lane is drawn with the same shift (never snapped onto the main
 *   straight). Its raw position stays the source; the shift is only for drawing.
 */
(function (root) {
  "use strict";

  function nearest(p, poly, closed) {
    let best = { d: Infinity, i: 0, t: 0, q: poly[0] };
    const n = poly.length;
    const segs = closed ? n : n - 1;
    for (let i = 0; i < segs; i++) {
      const a = poly[i], b = poly[(i + 1) % n];
      const dx = b[0] - a[0], dy = b[1] - a[1];
      const L2 = dx * dx + dy * dy;
      let t = L2 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2 : 0;
      t = Math.max(0, Math.min(1, t));
      const qx = a[0] + dx * t, qy = a[1] + dy * t;
      const d = Math.hypot(p[0] - qx, p[1] - qy);
      if (d < best.d) best = { d, i, t, q: [qx, qy], dir: [dx, dy] };
    }
    return best;
  }

  function smoothstep(x) { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); }

  /** offsets [[dx,dy]] (screen px) for every pit point; pit/track in screen coordinates.
   *  The shift is along the pit lane's own normal (smooth, it turns with the lane) - not along the
   *  normal of the nearest track segment, which jumps where that segment changes (e.g. in a
   *  corner at the pit exit, seen on real Baku data) and would put a step into the drawing. */
  function separation(pit, track, minSep, maxShift, taperPx) {
    const n = pit.length;
    const zero = pit.map(() => [0, 0]);
    if (n < 3 || !track || track.length < 3) return zero;
    const normals = pit.map((p, i) => {
      const a = pit[Math.max(0, i - 2)], b = pit[Math.min(n - 1, i + 2)];
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      return [-(b[1] - a[1]) / L, (b[0] - a[0]) / L];
    });
    const info = pit.map((p, i) => {
      const nr = nearest(p, track, true);
      const side = (p[0] - nr.q[0]) * normals[i][0] + (p[1] - nr.q[1]) * normals[i][1];
      return { d: nr.d, side };
    });
    // the side of the track the pit lane is on (weighted by how clearly it is on that side)
    let w = 0;
    for (const f of info) w += Math.max(-minSep, Math.min(minSep, f.side));
    const S = w >= 0 ? 1 : -1;
    const arc = [0];
    for (let i = 1; i < n; i++) arc.push(arc[i - 1] + Math.hypot(pit[i][0] - pit[i - 1][0], pit[i][1] - pit[i - 1][1]));
    const total = arc[n - 1] || 1;
    const taper = Math.max(8, Math.min(taperPx || 40, total * 0.3));
    let shift = info.map((f, i) => {
      // clearance = distance to the track on the lane's own side (0 if it is on the wrong side)
      const clear = f.side * S > 0 ? f.d : 0;
      const want = Math.max(0, Math.min(maxShift, minSep - clear));
      return want * smoothstep(arc[i] / taper) * smoothstep((total - arc[i]) / taper);
    });
    let vec = shift.map((v, i) => [normals[i][0] * S * v, normals[i][1] * S * v]);
    // smooth the offset vectors along the lane (no kinks), keep the ends at zero, cap the length
    for (let pass = 0; pass < 3; pass++) {
      const out = vec.map((v) => v.slice());
      for (let i = 1; i < n - 1; i++) {
        let sx = 0, sy = 0, c = 0;
        for (let k = -3; k <= 3; k++) { const j = i + k; if (j >= 0 && j < n) { sx += vec[j][0]; sy += vec[j][1]; c++; } }
        const L = Math.hypot(sx / c, sy / c), f = L > maxShift ? maxShift / L : 1;
        out[i] = [sx / c * f, sy / c * f];
      }
      out[0] = [0, 0]; out[n - 1] = [0, 0];
      vec = out;
    }
    return vec;
  }

  /** index of the pit point a car is drawn at (-1 = on the main track). Raw feed coordinates. */
  function carOnPit(p, pitRaw, trackRaw, inPit, maxDist) {
    if (!pitRaw || pitRaw.length < 2) return -1;
    const bb = pitRaw._bb || (pitRaw._bb = pitRaw.reduce((b, q) => [Math.min(b[0], q[0]), Math.min(b[1], q[1]),
      Math.max(b[2], q[0]), Math.max(b[3], q[1])], [Infinity, Infinity, -Infinity, -Infinity]));
    const m = maxDist || 150;
    if (p[0] < bb[0] - m || p[0] > bb[2] + m || p[1] < bb[1] - m || p[1] > bb[3] + m) return -1;
    let bi = -1, bd = Infinity;
    for (let i = 0; i < pitRaw.length; i++) {
      const d = Math.hypot(p[0] - pitRaw[i][0], p[1] - pitRaw[i][1]);
      if (d < bd) { bd = d; bi = i; }
    }
    if (bd > m) return -1;
    if (inPit) return bi;
    const dt = trackRaw && trackRaw.length > 2 ? nearest(p, trackRaw, true).d : Infinity;
    return bd < dt ? bi : -1;
  }

  root.PitLane = { separation, carOnPit, nearest };
})(typeof window !== "undefined" ? window : globalThis);

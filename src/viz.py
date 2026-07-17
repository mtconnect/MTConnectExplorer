"""
3D visualization for the Simulator page.

Builds a self-contained HTML page (rendered via ``st.iframe``) that shows an
articulated 3D model of the running machine and animates the *same* canned
routine the Python simulator is emitting. The routine description
(``RoutineSpec.to_dict()``) plus the current cycle phase are embedded as JSON;
the JavaScript re-runs the identical piecewise-linear interpolation, so the
on-screen motion is phase-locked to the live data with no Streamlit round-trips
(the view stays smooth and interactive — orbit / pan / zoom via OrbitControls).

three.js (+ OrbitControls) is vendored under ``src/static`` and inlined so the
view works fully offline.

Future model import: the JS ``MACHINES[type]`` builder returns
``{ root, apply(state) }``. The default builders here create primitive geometry.
A future ``buildFromModel(url, rig)`` can return the same shape (loading a
``.glb``/``.gltf`` via GLTFLoader and mapping named nodes — ``axis_x``…,
``joint_1``…), and the per-frame ``apply(state)`` logic is unchanged.
"""

import functools
import json
from pathlib import Path

from .simulator import RoutineSpec

_STATIC = Path(__file__).parent / "static"


@functools.lru_cache(maxsize=4)
def _read_static(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def render_equipment_html(spec: RoutineSpec, start_epoch: float, height: int = 540) -> str:
    """Return a self-contained HTML page animating ``spec``, phase-locked to the wall clock.

    ``start_epoch`` is the simulator's Unix start time (``EquipmentSimulator.start_epoch``).
    The page derives the current cycle phase from ``Date.now()`` every frame, exactly as the
    simulator derives it from ``time.time()`` — so the animation stays in sync with the
    emitted data with no drift, regardless of how long the iframe took to load or whether the
    tab was backgrounded. (The previous ``phase0`` snapshot advanced by ``performance.now()``
    started behind by the iframe's load time and drifted while backgrounded, which is why
    closing and reopening the view "fixed" it — it recaptured a fresh phase.)
    """
    payload = json.dumps({
        "spec": spec.to_dict(),
        "start_epoch": float(start_epoch),
        "cycle": spec.cycle_time,
    })
    return (
        _HTML_TEMPLATE
        .replace("/*__THREE__*/", _read_static("three.min.js"))
        .replace("/*__ORBIT__*/", _read_static("OrbitControls.js"))
        .replace("__PAYLOAD__", payload)
        .replace("__HEIGHT__", str(int(height)))
    )


# The JS body avoids Python str.format/f-strings (it is full of braces); values
# are injected via the unique markers above.
_HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  html, body { margin: 0; padding: 0; overflow: hidden; background: #1a1c22; }
  #wrap { position: relative; width: 100%; height: __HEIGHT__px; }
  #hud {
    position: absolute; top: 8px; left: 10px; color: #d8dbe2;
    font: 12px/1.4 -apple-system, Segoe UI, Roboto, sans-serif; pointer-events: none;
    text-shadow: 0 1px 2px #000; white-space: pre;
  }
  #hint {
    position: absolute; bottom: 8px; right: 10px; color: #8b90a0;
    font: 11px -apple-system, Segoe UI, Roboto, sans-serif; pointer-events: none;
  }
  #err {
    position: absolute; inset: 0; display: none; place-items: center; text-align: center;
    color: #e6e8ee; font: 13px -apple-system, Segoe UI, Roboto, sans-serif; padding: 20px;
  }
</style>
</head>
<body>
<div id="wrap">
  <div id="hud"></div>
  <div id="hint">middle-drag: rotate &nbsp;•&nbsp; right-drag: pan &nbsp;•&nbsp; scroll: zoom</div>
  <div id="err"></div>
</div>

<script>/*__THREE__*/</script>
<script>/*__ORBIT__*/</script>
<script>
"use strict";
var DATA = __PAYLOAD__;

function fail(msg){ var e=document.getElementById('err'); e.style.display='grid'; e.textContent=msg; }

try {
  if (typeof THREE === 'undefined') throw new Error('3D library failed to load.');

  var spec = DATA.spec;
  var CYCLE = DATA.cycle;
  var wrap = document.getElementById('wrap');
  var W = wrap.clientWidth || 640, H = wrap.clientHeight || __HEIGHT__;

  var renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  renderer.setSize(W, H);
  wrap.appendChild(renderer.domElement);

  var scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1a1c22);

  // Machine coordinates are millimetres, Z-up. Tell the camera Z is up so
  // OrbitControls orbits naturally.
  var camera = new THREE.PerspectiveCamera(45, W / H, 1, 20000);
  camera.up.set(0, 0, 1);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  var dir = new THREE.DirectionalLight(0xffffff, 0.9);
  dir.position.set(600, -900, 1400);
  scene.add(dir);
  var dir2 = new THREE.DirectionalLight(0xffffff, 0.35);
  dir2.position.set(-800, 600, 400);
  scene.add(dir2);

  var controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  // Middle-drag rotates, right-drag pans, wheel zooms (left also rotates as a
  // convenience for mice/trackpads without a usable middle button).
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.ROTATE,
    RIGHT: THREE.MOUSE.PAN
  };

  // ---- deg/rad + interpolation (mirror of RoutineSpec.coords in Python) ----
  function d2r(d){ return d * Math.PI / 180; }
  function coords(phase){
    phase = ((phase % CYCLE) + CYCLE) % CYCLE;
    var prev = spec.start, acc = 0.0;
    var moves = spec.moves;
    for (var i = 0; i < moves.length; i++){
      var mv = moves[i];
      if (phase < acc + mv.duration || i === moves.length - 1){
        var t = mv.duration <= 0 ? 0 : (phase - acc) / mv.duration;
        t = Math.min(Math.max(t, 0), 1);
        var pos = prev.map(function(p, k){ return p + (mv.target[k] - p) * t; });
        return { pos: pos, meta: mv.meta };
      }
      acc += mv.duration; prev = mv.target;
    }
    return { pos: prev.slice(), meta: {} };
  }

  // ---- helpers -----------------------------------------------------------
  function box(w, d, h, color, opacity){
    var m = new THREE.MeshLambertMaterial({ color: color });
    if (opacity != null){ m.transparent = true; m.opacity = opacity; }
    return new THREE.Mesh(new THREE.BoxGeometry(w, d, h), m);
  }
  function zCylinder(radius, length, color){
    // cylinder along local +Z, base at origin, extending to +Z by `length`.
    var g = new THREE.CylinderGeometry(radius, radius, length, 20);
    g.rotateX(Math.PI / 2);           // default +Y axis -> +Z
    g.translate(0, 0, length / 2);
    return new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: color }));
  }

  // ---- machine builders --------------------------------------------------
  var MACHINES = {
    MILL_3AXIS: function(){
      var g = spec.geometry, wk = g.work;
      var cx = (wk.x0 + wk.x1) / 2, cy = (wk.y0 + wk.y1) / 2;
      var toolR = (g.tool_dia || 50) / 2;
      var root = new THREE.Group();

      // Fixed floor grid (X-Y plane).
      var grid = new THREE.GridHelper(g.limit, 20, 0x3a3f4b, 0x2a2e38);
      grid.rotation.x = Math.PI / 2;
      grid.position.set(g.limit / 2, g.limit / 2, 0);
      root.add(grid);

      // Fixed vertical gantry (portal): legs at the X-envelope edges (clear of
      // the sliding table) with a top beam; the spindle head rides up/down on it.
      var zBeam = wk.z_safe + 200, gmat = 0x565d6b;
      function leg(px){ var m = box(45, 45, zBeam, gmat); m.position.set(px, cy, zBeam / 2); root.add(m); }
      leg(0); leg(g.limit);
      var beam = box(g.limit + 90, 60, 60, gmat); beam.position.set(cx, cy, zBeam); root.add(beam);

      // Moving table (plate) carrying the workpiece; translates in X/Y so the
      // cut point stays under the fixed tool.
      var table = new THREE.Group(); root.add(table);
      var plate = box(wk.x1 - wk.x0 + 140, wk.y1 - wk.y0 + 140, 40, 0x4b5160);
      plate.position.set(cx, cy, wk.z_bot - 20); table.add(plate);
      var stock = box(wk.x1 - wk.x0, wk.y1 - wk.y0, wk.z_top - wk.z_bot, 0xe8ecf2, 0.92); // white block
      stock.position.set(cx, cy, (wk.z_bot + wk.z_top) / 2); table.add(stock);

      // Spindle head (white block) + tool, fixed in X/Y, moving only in Z.
      var HEAD_UP = 70;
      var head = box(100, 100, 64, 0xdfe3ea); root.add(head);
      var tool = zCylinder(toolR, HEAD_UP, 0xd9a441); root.add(tool);
      var tipDot = new THREE.Mesh(new THREE.SphereGeometry(Math.max(toolR * 0.6, 6), 16, 16),
        new THREE.MeshLambertMaterial({ color: 0xff5a4d })); root.add(tipDot);

      return {
        root: root,
        trailParent: table,   // trail is drawn in the part frame -> shows the cut pattern
        apply: function(state){
          var x = state.pos[0], y = state.pos[1], z = state.pos[2];
          table.position.set(cx - x, cy - y, 0);
          tool.position.set(cx, cy, z);
          head.position.set(cx, cy, z + HEAD_UP + 32);
          tipDot.position.set(cx, cy, z);
          return [x, y, z];   // table-local coords engrave the pattern on the moving part
        }
      };
    },

    ROBOT_6DOF: function(){
      var g = spec.geometry, links = g.links;
      var root = new THREE.Group();

      var grid = new THREE.GridHelper(1200, 24, 0x3a3f4b, 0x2a2e38);
      grid.rotation.x = Math.PI / 2;
      root.add(grid);
      // Pedestal.
      var ped = zCylinder(55, 40, 0x4b5160); root.add(ped);

      var colors = [0x6b7280, 0x8b93a3, 0x6b7280, 0x8b93a3, 0x6b7280, 0xc2c8d2];
      var groups = [];
      var parent = root;
      for (var i = 0; i < links.length; i++){
        var grp = new THREE.Group();
        grp.position.set(links[i].offset[0], links[i].offset[1], links[i].offset[2]);
        parent.add(grp);
        groups.push(grp);
        // link cylinder from this joint toward its child (or the tool for the last).
        var childOff = (i < links.length - 1) ? links[i + 1].offset : g.tool_offset;
        var L = Math.max(childOff[2], 1);
        grp.add(zCylinder(i < 3 ? 34 : 24, L, colors[i]));
        // knuckle
        var kn = new THREE.Mesh(new THREE.SphereGeometry(i < 3 ? 40 : 28, 16, 16),
          new THREE.MeshLambertMaterial({ color: 0x3f4552 }));
        grp.add(kn);
        parent = grp;
      }
      // Tool flange + two gripper fingers at the TCP.
      var tool = new THREE.Group();
      tool.position.set(g.tool_offset[0], g.tool_offset[1], g.tool_offset[2]);
      groups[groups.length - 1].add(tool);
      var fingerL = 45, fingerMat = 0xd9a441;
      var f1 = box(10, 12, fingerL, fingerMat); f1.position.set(22, 0, fingerL / 2); tool.add(f1);
      var f2 = box(10, 12, fingerL, fingerMat); f2.position.set(-22, 0, fingerL / 2); tool.add(f2);
      var tipDot = new THREE.Mesh(new THREE.SphereGeometry(10, 16, 16),
        new THREE.MeshLambertMaterial({ color: 0xff5a4d }));
      tool.add(tipDot);

      var tipWorld = new THREE.Vector3();
      return {
        root: root,
        apply: function(state){
          for (var i = 0; i < 6; i++){
            var ax = links[i].axis, a = d2r(state.pos[i]);
            groups[i].rotation.x = ax === 'x' ? a : 0;
            groups[i].rotation.y = ax === 'y' ? a : 0;
            groups[i].rotation.z = ax === 'z' ? a : 0;
          }
          var open = (state.meta && state.meta.grip === 'CLOSED') ? 9 : 22;
          f1.position.x = open; f2.position.x = -open;
          root.updateMatrixWorld(true);
          tipDot.getWorldPosition(tipWorld);
          return [tipWorld.x, tipWorld.y, tipWorld.z];
        }
      };
    }
  };

  var machine = MACHINES[spec.equipment]();
  scene.add(machine.root);

  // Frame the model.
  var bounds = new THREE.Box3().setFromObject(machine.root);
  var center = bounds.getCenter(new THREE.Vector3());
  var size = bounds.getSize(new THREE.Vector3());
  var reach = Math.max(size.x, size.y, size.z) || 1000;
  controls.target.copy(center);
  camera.position.set(center.x + reach * 1.1, center.y - reach * 1.3, center.z + reach * 0.9);
  camera.lookAt(center);

  // Origin triad / gizmo: X = red, Y = green, Z = blue (up). depthTest off so it
  // is always readable, with X/Y/Z sprite labels at the axis tips.
  var axisLen = reach * 0.28;
  var axes = new THREE.AxesHelper(axisLen);
  axes.material.depthTest = false;
  axes.renderOrder = 998;
  scene.add(axes);
  function axisLabel(text, color, pos){
    var cv = document.createElement('canvas'); cv.width = 64; cv.height = 64;
    var g = cv.getContext('2d');
    g.fillStyle = color; g.font = 'bold 46px sans-serif';
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText(text, 32, 34);
    var sp = new THREE.Sprite(new THREE.SpriteMaterial({
      map: new THREE.CanvasTexture(cv), depthTest: false, transparent: true }));
    sp.position.set(pos[0], pos[1], pos[2]);
    var s = reach * 0.1; sp.scale.set(s, s, 1);
    sp.renderOrder = 1000;
    scene.add(sp);
  }
  var lt = axisLen * 1.12;
  axisLabel('X', '#ff5566', [lt, 0, 0]);
  axisLabel('Y', '#55dd66', [0, lt, 0]);
  axisLabel('Z', '#5599ff', [0, 0, lt]);

  // Tool-path trail (reset when the cycle wraps). Points are decimated by
  // distance so one full program cycle fits within the buffer. It is rendered
  // in the transparent pass with a high renderOrder and depthTest/depthWrite
  // off, so it always draws ON TOP of everything (including the workpiece it
  // cuts into). For the mill the trail is parented to the moving table so it
  // engraves the part.
  var MAX_TRAIL = 6000;
  var trailPos = new Float32Array(MAX_TRAIL * 3);
  var trailGeo = new THREE.BufferGeometry();
  trailGeo.setAttribute('position', new THREE.BufferAttribute(trailPos, 3));
  trailGeo.setDrawRange(0, 0);
  var trail = new THREE.Line(trailGeo, new THREE.LineBasicMaterial({
    color: 0xff5a4d, transparent: true, depthTest: false, depthWrite: false }));
  trail.renderOrder = 1001;
  (machine.trailParent || scene).add(trail);
  var trailN = 0, lastPhase = -1, lastAdd = [1e9, 1e9, 1e9];

  var hud = document.getElementById('hud');

  function fmt(n){ return (Math.round(n * 10) / 10).toString(); }

  function frame(){
    // Phase-locked to wall clock (Date.now) — the same reference the simulator uses
    // (time.time()) — recomputed every frame, so the animation never drifts from the
    // emitted data regardless of iframe load time or tab backgrounding.
    var phase = ((((Date.now() / 1000) - DATA.start_epoch) % CYCLE) + CYCLE) % CYCLE;
    var st = coords(phase);
    var tip = machine.apply(st);

    if (phase < lastPhase){ trailN = 0; lastAdd = [1e9, 1e9, 1e9]; }   // wrapped -> clear
    lastPhase = phase;
    if (trailN < MAX_TRAIL){
      var dx = tip[0] - lastAdd[0], dy = tip[1] - lastAdd[1], dz = tip[2] - lastAdd[2];
      if (trailN === 0 || (dx * dx + dy * dy + dz * dz) > 9){    // decimate: > 3 mm
        trailPos[trailN * 3] = tip[0];
        trailPos[trailN * 3 + 1] = tip[1];
        trailPos[trailN * 3 + 2] = tip[2];
        trailN++;
        lastAdd = tip;
        trailGeo.setDrawRange(0, trailN);
        trailGeo.attributes.position.needsUpdate = true;
      }
    }

    var m = st.meta || {};
    var label = (spec.equipment === 'MILL_3AXIS')
      ? ('MILL  ' + (m.prog || '') + '\nX ' + fmt(st.pos[0]) + '  Y ' + fmt(st.pos[1]) + '  Z ' + fmt(st.pos[2]) + ' mm')
      : ('ROBOT  ' + (m.prog || '') + '  grip:' + (m.grip || '') + '\nJ ' + st.pos.map(fmt).join('  ') + ' deg');
    hud.textContent = label + '\ncycle ' + fmt(phase) + ' / ' + fmt(CYCLE) + ' s';

    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  window.addEventListener('resize', function(){
    var w = wrap.clientWidth || W, h = wrap.clientHeight || H;
    camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h);
  });
} catch (e){
  fail('Visualization unavailable: ' + (e && e.message ? e.message : e));
}
</script>
</body>
</html>
"""

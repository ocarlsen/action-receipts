// tour/overlay.ts
//
// The on-page layer the walkthrough film draws with: a caption bubble that sits beside the element
// it is about (with a pointer), a highlight frame on that element, a cursor that travels between
// targets, a click pulse, and a full-screen title card for the start and end. It is injected into
// every page as a plain script (Playwright addInitScript) and exposes window.tourUI. Nothing in it
// takes pointer events, so the page underneath behaves normally.

const STYLE = `
#tui{position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:500 16px/1.45 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
#tui .bubble{position:absolute;box-sizing:border-box;padding:12px 16px;border-radius:10px;background:#16201c;color:#f1f4f2;
  box-shadow:0 10px 30px rgba(0,0,0,.35);opacity:0;transition:opacity .25s}
#tui .bubble.show{opacity:1}
#tui .tip{position:absolute;width:0;height:0;border:9px solid transparent}
#tui .frame{position:absolute;border:2px solid #2f8f6f;border-radius:8px;background:rgba(47,143,111,.06);opacity:0;
  transition:opacity .2s,left .4s,top .4s,width .4s,height .4s}
#tui .frame.show{opacity:1}
#tui .cursor{position:absolute;left:0;top:0;width:22px;height:22px;transition-property:transform;transition-timing-function:ease-in-out}
#tui .pulse{position:absolute;width:30px;height:30px;margin:-15px 0 0 -15px;border-radius:50%;background:rgba(47,143,111,.35);
  animation:tuiPulse .55s ease-out forwards}
@keyframes tuiPulse{from{transform:scale(.3);opacity:1}to{transform:scale(1.6);opacity:0}}
#tui .card{position:absolute;inset:0;background:#101412;color:#f1f4f2;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:12px;opacity:0;transition:opacity .4s}
#tui .card.show{opacity:1}
#tui .card b{font-size:32px;letter-spacing:-.01em}
#tui .card span{font-size:17px;color:#9fb0a8}
`;

const SCRIPT = `
(() => {
  if (window.tourUI) return;
  const css = ${JSON.stringify(STYLE)};
  let root, bubble, tip, frame, cursor, card;
  let at = { x: 720, y: 450 };
  function build() {
    if (document.getElementById("tui")) return;
    const s = document.createElement("style"); s.textContent = css; document.head.appendChild(s);
    root = document.createElement("div"); root.id = "tui";
    root.innerHTML = '<div class="frame"></div><div class="bubble"><div class="tip"></div><div class="text"></div></div>' +
      '<svg class="cursor" viewBox="0 0 22 22"><path d="M3 2v17l4.6-4.2 3.2 6.4 2.8-1.4-3.1-6.2H17z" fill="#fff" stroke="#16201c" stroke-width="1.5" stroke-linejoin="round"/></svg>' +
      '<div class="card"><b></b><span></span></div>';
    document.body.appendChild(root);
    bubble = root.querySelector(".bubble"); tip = root.querySelector(".tip"); frame = root.querySelector(".frame");
    cursor = root.querySelector(".cursor"); card = root.querySelector(".card");
    cursor.style.transitionDuration = "0ms";
    cursor.style.transform = "translate(" + at.x + "px," + at.y + "px)";
  }
  function overlaps(a, b) { return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h; }
  window.tourUI = {
    build,
    /** Size a bubble for this text would have, without showing it. */
    size(text, width) {
      build();
      bubble.classList.remove("show");
      bubble.style.width = "auto"; bubble.style.maxWidth = (width || 380) + "px";
      bubble.querySelector(".text").textContent = text;
      bubble.style.left = "-5000px"; bubble.style.top = "0";
      return { w: bubble.offsetWidth, h: bubble.offsetHeight };
    },
    /** Show text beside target {x,y,w,h} (or low and centred when null). prefer: "below"|"above"|"right"|"left".
     *  Tries prefer, then below, above, right, left; the first spot inside the viewport that does not cover the
     *  target wins. Returns where it went: {side, x, y, w, h}; side "over" means nothing fitted. */
    say(text, target, prefer, width) {
      const { w, h } = this.size(text, width);
      const W = innerWidth, H = innerHeight, m = 12, g = 16;
      let pick = null;
      if (target) {
        const cx = Math.max(m, Math.min(W - w - m, target.x + Math.min(target.w, 400) / 2 - w / 2));
        const cy = Math.max(m, Math.min(H - h - m, target.y + Math.min(target.h, 90) / 2 - h / 2));
        const spots = {
          below: { x: cx, y: target.y + target.h + g }, above: { x: cx, y: target.y - g - h },
          right: { x: target.x + target.w + g, y: cy }, left: { x: target.x - g - w, y: cy },
        };
        for (const side of [prefer, "below", "above", "right", "left"]) {
          const p = side && spots[side];
          if (!p) continue;
          const box = { x: p.x, y: p.y, w, h };
          if (box.x >= m && box.y >= m && box.x + w <= W - m && box.y + h <= H - m && !overlaps(box, target)) { pick = { side, ...p }; break; }
        }
      }
      if (!pick) pick = { side: target ? "over" : "none", x: (W - w) / 2, y: H - h - 40 };
      bubble.style.left = pick.x + "px"; bubble.style.top = pick.y + "px";
      tip.style.cssText = "display:none";
      if (target && pick.side !== "over") {
        const ax = Math.max(14, Math.min(w - 30, target.x + Math.min(target.w, 400) / 2 - pick.x - 9));
        const ay = Math.max(10, Math.min(h - 28, target.y + Math.min(target.h, 90) / 2 - pick.y - 9));
        const c = "#16201c";
        if (pick.side === "below") tip.style.cssText = "left:" + ax + "px;top:-18px;border-bottom-color:" + c;
        if (pick.side === "above") tip.style.cssText = "left:" + ax + "px;bottom:-18px;border-top-color:" + c;
        if (pick.side === "right") tip.style.cssText = "top:" + ay + "px;left:-18px;border-right-color:" + c;
        if (pick.side === "left") tip.style.cssText = "top:" + ay + "px;right:-18px;border-left-color:" + c;
      }
      requestAnimationFrame(() => bubble.classList.add("show"));
      return { side: pick.side, x: pick.x, y: pick.y, w, h };
    },
    /** Frame a box {x,y,w,h}, or remove the frame (null). */
    highlight(box) {
      build();
      if (!box) { frame.classList.remove("show"); return; }
      Object.assign(frame.style, { left: box.x - 5 + "px", top: box.y - 5 + "px", width: box.w + 10 + "px", height: box.h + 10 + "px" });
      frame.classList.add("show");
    },
    point(x, y, ms) {
      build();
      cursor.style.transitionDuration = (ms == null ? 600 : ms) + "ms";
      cursor.style.transform = "translate(" + x + "px," + y + "px)";
      at = { x, y };
    },
    click(x, y) {
      build();
      const p = document.createElement("div"); p.className = "pulse"; p.style.left = x + "px"; p.style.top = y + "px";
      root.appendChild(p); setTimeout(() => p.remove(), 600);
    },
    hide() { build(); bubble.classList.remove("show"); frame.classList.remove("show"); },
    /** Full-screen title card: on (instantly when fade is 0) or off. */
    title(on, heading, sub, fadeMs) {
      build();
      if (heading != null) { card.querySelector("b").textContent = heading; card.querySelector("span").textContent = sub || ""; }
      card.style.transitionDuration = (fadeMs == null ? 400 : fadeMs) + "ms";
      card.classList.toggle("show", on);
      cursor.style.display = on && fadeMs !== 0 ? "none" : "";
    },
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
})();
`;

export const OVERLAY_SCRIPT: string = SCRIPT;

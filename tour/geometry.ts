// tour/geometry.ts
//
// What --check holds every caption to: inside the viewport, and not covering the element it is
// about. Pure functions on plain boxes.

export type Rect = { x: number; y: number; w: number; h: number };

export const overlaps = (a: Rect, b: Rect): boolean =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

export const fitsIn = (r: Rect, width: number, height: number): boolean =>
  r.x >= 0 && r.y >= 0 && r.x + r.w <= width && r.y + r.h <= height;

/** Reasons a placed caption is wrong; an empty list means it is fine. */
export function placementProblems(caption: Rect, target: Rect | null, width: number, height: number): string[] {
  const out: string[] = [];
  if (!fitsIn(caption, width, height)) out.push("caption leaves the viewport");
  if (target && overlaps(caption, target)) out.push("caption covers its target");
  return out;
}

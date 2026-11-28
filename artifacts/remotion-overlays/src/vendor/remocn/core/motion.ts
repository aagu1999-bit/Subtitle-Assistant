/* Vendored from https://github.com/Remocn/remocn (MIT).
 *
 * MIT License, Copyright (c) 2026 Remocn. The licence requires the notice to
 * travel with the code: the full text is in src/vendor/remocn/LICENSE.
 *
 * Not installed with the shadcn CLI: remocn.dev is unreachable from this
 * build environment (the egress proxy refuses it), so these files came from
 * raw.githubusercontent.com. The ONLY edit is the import paths -- remocn
 * ships "@/lib/..." aliases and this project has no "@/" alias, so they are
 * rewritten relative. Nothing else is changed, so updating is a re-download
 * plus the same rewrite.
 */
export const easings = {
  linear: (t: number): number => t,
  out: (t: number): number => 1 - (1 - t) ** 3,
  in: (t: number): number => t * t * t,
  inOut: (t: number): number =>
    t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2,
} as const;

export type EasingName = keyof typeof easings;

export const springs = {
  snappy: { damping: 18, stiffness: 220, mass: 0.7 },
  soft: { damping: 14, stiffness: 120, mass: 0.9 },
  bouncy: { damping: 10, stiffness: 180, mass: 0.8 },
} as const;

export type SpringName = keyof typeof springs;

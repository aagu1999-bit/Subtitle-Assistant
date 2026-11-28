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
export interface Step<S extends string = string> {
  at: number;
  state: S;
  duration?: number;
}

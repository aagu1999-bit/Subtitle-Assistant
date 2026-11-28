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
// color.ts is NOT vendored. It is the only file in remocn's tree that needs
// `culori`, and nothing the X Follow Card reaches uses any of these: cursor
// takes the theme, use-cursor-path takes the easings. Re-exporting them here
// would pull an npm dependency in for code that never runs. If a future
// remocn look needs oklch mixing, vendor color.ts and add culori then.
export type { EasingName, SpringName } from "./motion";
export { easings, springs } from "./motion";
export type { RemocnTheme, RemocnUIProviderProps } from "./theme";
export {
  defaultDarkTheme,
  defaultLightTheme,
  RemocnUIProvider,
  useRemocnTheme,
} from "./theme";
export type { TypewriterOptions, TypewriterState } from "./timeline";
export {
  clamp01,
  framesFor,
  revealCount,
  revealedText,
  useCurrentState,
  useStateTransition,
  useTypewriter,
} from "./timeline";
export type { Step } from "./types";

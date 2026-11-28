/**
 * The one look, on the React side.
 *
 * The authority is artifacts/subtitle-burner/tokens.py. These are the same
 * values, and they are here only so a composition has something to draw with
 * when it is opened in Remotion Studio with no props -- at render time the
 * real values arrive as props, because a project can change the brand colour
 * and the font and this file cannot know that.
 *
 * THE RULE, enforced by a test: no composition in src/ may write `brand` or
 * `accent` as a hex literal. Those two are what the editor lets a project
 * change, so they have to arrive as props. This file is the only place their
 * literals may appear.
 *
 * Decorative colours -- greys, the polaroid browns, pure white type -- may
 * stay literal. Nothing in the editor sets them, so a token would be a lie
 * about how configurable they are.
 *
 * Why this exists: _remotion_look_props answered "#FFD60A" as a literal three
 * times over. It matched the default, so every render looked correct; the
 * moment someone set a pink highlight, devices.py went pink and these
 * compositions carried on in yellow. The two caption looks were worse -- they
 * drew in '#2563eb', Tailwind's default blue, which this brand never chose.
 */

export type Tokens = {
  brandColor: string;
  accentColor: string;
  inkColor: string;
  surfaceColor: string;
  displayFont: string;
  uiFont: string;
  radius: number;
  fadeOutS: number;
};

/** Studio fallbacks. Must stay equal to tokens.py; a test checks that. */
export const TOKENS: Tokens = {
  brandColor: "#FFD60A",
  accentColor: "#00E5FF",
  inkColor: "#F8FAFC",   // off-white; see tokens.py for why not pure white
  surfaceColor: "#0F1220",
  displayFont: "Anton",
  uiFont: "DM Sans",
  radius: 18,
  fadeOutS: 0.3,
};

/** Props as given, with anything absent filled from the fallbacks. */
export const withTokens = (props?: Partial<Tokens>): Tokens => ({
  ...TOKENS,
  ...Object.fromEntries(
    Object.entries(props ?? {}).filter(([, v]) => v !== undefined && v !== null),
  ),
});

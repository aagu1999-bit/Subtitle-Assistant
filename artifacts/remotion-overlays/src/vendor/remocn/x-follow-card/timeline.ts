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
import { defineTimeline, type TimelineElement } from "../lib/timeline";

// The profile card bouncing in, its layers blurring in top to bottom, the
// cursor travelling to the Follow button and the click that flips it to
// Following. Frames on the 30 fps scene timeline, after `speed`. The ids are
// public: a saved `timing` prop refers to them.

export type XFollowCardElementId =
  | "card"
  | "cover"
  | "avatar"
  | "name"
  | "handle"
  | "bio"
  | "meta"
  | "buttons"
  | "tabs"
  | "post"
  | "cursor"
  | "follow";

/** The scene's timing does not depend on its props. */
export type XFollowCardTimingProps = object;

/** The frame the cursor clicks Follow. */
export const CLICK_AT = 110;
/** The card's layers, top to bottom, in the order they blur in. */
export const CARD_LAYERS = [
  "cover",
  "avatar",
  "name",
  "handle",
  "bio",
  "meta",
  "buttons",
  "tabs",
  "post",
] as const;
const LAYER_AT = 20;
const LAYER_STEP = 2;
const LAYER_IN = 6;
const CURSOR_TRAVEL = 32;

// `card` is the spring that bounces the card in. Each layer blurs in over
// its length; the layers are a chain. `cursor` travels to the button and
// ends on the click, resting `rest` before it sets off. `follow` flips the
// button; the cursor holds there for `hold`.
export const xFollowCardTimeline = defineTimeline<
  XFollowCardElementId,
  XFollowCardTimingProps
>((): TimelineElement<XFollowCardElementId>[] => [
  {
    id: "card",
    lane: "Card",
    label: "card bounces in",
    start: 0,
    length: LAYER_AT,
  },
  ...CARD_LAYERS.map(
    (id, g): TimelineElement<XFollowCardElementId> => ({
      id,
      lane: "Layers",
      label: id,
      start: LAYER_AT + g * LAYER_STEP,
      length: LAYER_IN,
      follows: g > 0 ? CARD_LAYERS[g - 1] : "card",
    }),
  ),
  {
    id: "cursor",
    lane: "Cursor",
    label: "cursor to Follow",
    start: CLICK_AT - CURSOR_TRAVEL,
    length: CURSOR_TRAVEL,
    beats: { rest: -3 },
  },
  {
    id: "follow",
    lane: "Button",
    label: "Following",
    start: CLICK_AT,
    length: 10,
    follows: "cursor",
    beats: { hold: 30 },
  },
]);

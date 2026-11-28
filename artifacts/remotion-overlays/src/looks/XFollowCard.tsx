import { type Tokens, withTokens } from "../tokens";
import React from "react";
import { XFollowCard, type XFollowCardProps } from "../vendor/remocn/x-follow-card";

/** remocn's X Follow Card, as a 9:16 overlay.
 *
 * No LookFrame here, and that is deliberate: the vendored component already
 * reads the composition size and scales its own 720x1280 reference stage to
 * fit, so wrapping it in our centring frame would scale it twice. Its root
 * is already a transparent AbsoluteFill.
 *
 * `orientation` is forced to vertical. remocn's horizontal mode lays out on
 * 1280x720, which in a 1080x1920 frame scales to about 0.84 and leaves empty
 * bands above and below; vertical is 720x1280, an exact 1.5x here.
 *
 * The brand arrives as `accentColor` so the Follow button and the links
 * follow the project rather than remocn's Twitter blue. Everything else is
 * content the app supplies, or remocn's own default when it does not.
 */
export const XFollowCardLook: React.FC<Partial<Tokens> & XFollowCardProps> = (
  props,
) => {
  const t = withTokens(props);
  return (
    <XFollowCard
      {...props}
      orientation="vertical"
      accentColor={props.accentColor ?? t.brandColor}
      // remocn defaults avatarUrl to "/logo.svg" and coverUrl to its own
      // image, neither of which exists in this project's public/ folder --
      // the render logged a 404 per frame and fell back anyway. Empty
      // strings take the component's own gradient fallbacks cleanly, and a
      // real picture arrives when the app stages one.
      avatarUrl={props.avatarUrl ?? ""}
      coverUrl={props.coverUrl ?? ""}
    />
  );
};

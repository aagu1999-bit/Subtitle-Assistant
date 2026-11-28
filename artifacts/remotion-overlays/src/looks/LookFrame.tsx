import React from "react";
import { AbsoluteFill } from "remotion";
import { withTokens, type Tokens } from "../tokens";

/** Transparent 9:16 host so official Elements sit over the talking head.
 *
 * It also publishes the project's look as CSS custom properties. Custom
 * properties rather than props because several of these Elements use the
 * brand inside a nested subcomponent -- the bar in VerticalBarChart, the
 * active word in the caption looks -- and drilling a prop to each of those
 * is far more code than one declaration that cascades. It is the same shape
 * as tokens.as_css_vars on the Python side, so there is one mechanism, not
 * two.
 */
export const LookFrame: React.FC<{
  children: React.ReactNode;
  align?: "center" | "lower" | "bottom";
  scale?: number;
  tokens?: Partial<Tokens>;
}> = ({ children, align = "center", scale = 1, tokens }) => {
  const t = withTokens(tokens);
  return (
  <AbsoluteFill
    style={{
      ["--brand" as string]: t.brandColor,
      ["--accent" as string]: t.accentColor,
      ["--ink" as string]: t.inkColor,
      ["--surface" as string]: t.surfaceColor,
      ["--display-font" as string]: t.displayFont,
      background: "transparent",
      alignItems: "center",
      justifyContent:
        align === "bottom" ? "flex-end" : align === "lower" ? "flex-end" : "center",
      paddingBottom: align === "bottom" ? 160 : align === "lower" ? 280 : 0,
    }}
  >
    <div
      style={
        scale !== 1
          ? { transform: `scale(${scale})`, transformOrigin: "center" }
          : undefined
      }
    >
      {children}
    </div>
  </AbsoluteFill>
  );
};

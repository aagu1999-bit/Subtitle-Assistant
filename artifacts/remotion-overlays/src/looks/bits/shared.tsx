import { TOKENS } from "../../tokens";
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";

// From ../../tokens, the one place the brand is declared on this side.
// These were literals, so a project that set a pink highlight got pink
// devices and gold Bits in the same video.
export const GOLD = TOKENS.brandColor;
export const MINT = "#00FF88";      // decorative, not settable
export const CYAN = TOKENS.accentColor;
export const FONT = "Inter, Anton, system-ui, sans-serif";

export const BitsStage: React.FC<{
  children: React.ReactNode;
  opaque?: boolean;
}> = ({ children, opaque }) => (
  <AbsoluteFill
    style={{
      background: opaque ? "#07070c" : "transparent",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: FONT,
      overflow: "hidden",
    }}
  >
    {children}
  </AbsoluteFill>
);

export const useClock = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  return { frame, fps, durationInFrames };
};

export const GoldTitle: React.FC<{
  children: React.ReactNode;
  color?: string;
  style?: React.CSSProperties;
}> = ({ children, color = GOLD, style }) => (
  <div
    style={{
      color,
      fontWeight: 800,
      fontSize: 72,
      letterSpacing: -1.2,
      textAlign: "center",
      lineHeight: 1.05,
      maxWidth: 960,
      ...style,
    }}
  >
    {children}
  </div>
);

export type BitsTextProps = { text?: string; color?: string; duration?: number };
export type BitsCountProps = { to?: number; color?: string; duration?: number };
export type BitsWordsProps = { words?: string[]; color?: string; duration?: number };
export type BitsCardProps = { labels?: string[]; text?: string; duration?: number };

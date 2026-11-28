import { TOKENS } from "../tokens";
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";

/** Bits-style split title, driven by time instead of scroll/hover. */
export const SplitTextLook: React.FC<{ text?: string; color?: string }> = ({
  text = "WATCH THIS",
  color = TOKENS.brandColor,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const chars = Array.from(text);
  return (
    <AbsoluteFill style={{
      background: "transparent",
      alignItems: "center",
      justifyContent: "center",
      flexDirection: "row",
      flexWrap: "wrap",
      fontFamily: "Inter, Anton, sans-serif",
      fontWeight: 800,
      fontSize: 72,
      letterSpacing: -1,
      color,
    }}>
      {chars.map((ch, i) => {
        const t0 = i * 2;
        const y = interpolate(frame, [t0, t0 + fps * 0.35], [40, 0], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp",
        });
        const op = interpolate(frame, [t0, t0 + fps * 0.25], [0, 1], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp",
        });
        return (
          <span key={i} style={{
            display: "inline-block",
            transform: `translateY(${y}px)`,
            opacity: op,
            whiteSpace: ch === " " ? "pre" : undefined,
          }}>{ch === " " ? "\u00a0" : ch}</span>
        );
      })}
    </AbsoluteFill>
  );
};

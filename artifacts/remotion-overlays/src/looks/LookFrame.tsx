import React from "react";
import { AbsoluteFill } from "remotion";

/** Transparent 9:16 host so official Elements sit over the talking head. */
export const LookFrame: React.FC<{
  children: React.ReactNode;
  align?: "center" | "lower" | "bottom";
  scale?: number;
}> = ({ children, align = "center", scale = 1 }) => (
  <AbsoluteFill
    style={{
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

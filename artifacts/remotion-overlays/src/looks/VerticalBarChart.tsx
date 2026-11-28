import { type Tokens } from "../tokens";
import { VerticalBarChart } from "./vertical-bar-chart.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const VerticalBarChartLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} scale={0.92}>
      <VerticalBarChart
        durationInFrames={durationInFrames}
        name="Vertical Bar Chart"
        from={0}
        style={{ position: "relative" }}
      />
    </LookFrame>
  );
};

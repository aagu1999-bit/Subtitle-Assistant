import { type Tokens } from "../tokens";
import { LineChart } from "./line-chart.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const LineChartLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} scale={0.92}>
      <LineChart
        durationInFrames={durationInFrames}
        name="Line Chart"
        from={0}
        style={{ position: "relative" }}
      />
    </LookFrame>
  );
};

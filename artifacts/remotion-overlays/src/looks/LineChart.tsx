import { LineChart } from "./line-chart.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const LineChartLook: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame scale={0.92}>
      <LineChart
        durationInFrames={durationInFrames}
        name="Line Chart"
        from={0}
        style={{ position: "relative" }}
      />
    </LookFrame>
  );
};

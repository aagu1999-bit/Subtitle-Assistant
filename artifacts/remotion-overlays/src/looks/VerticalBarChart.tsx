import { VerticalBarChart } from "./vertical-bar-chart.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const VerticalBarChartLook: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame scale={0.92}>
      <VerticalBarChart
        durationInFrames={durationInFrames}
        name="Vertical Bar Chart"
        from={0}
        style={{ position: "relative" }}
      />
    </LookFrame>
  );
};

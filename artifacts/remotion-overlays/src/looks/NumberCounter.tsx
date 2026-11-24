import { NumberCounter } from "./number-counter.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const NumberCounterLook: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame>
      <NumberCounter
        durationInFrames={durationInFrames}
        name="Number Counter"
        from={0}
        style={{ width: 640, height: 200, position: "relative" }}
      />
    </LookFrame>
  );
};

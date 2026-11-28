import { type Tokens } from "../tokens";
import { OnScreenMessages } from "./on-screen-messages.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const OnScreenMessagesLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} scale={0.82}>
      <OnScreenMessages
        durationInFrames={durationInFrames}
        name="On-Screen Messages"
        from={0}
        style={{ width: 1260, height: 680, position: "relative" }}
      />
    </LookFrame>
  );
};

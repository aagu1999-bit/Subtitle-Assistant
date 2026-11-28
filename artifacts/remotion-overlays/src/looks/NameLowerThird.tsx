import { type Tokens } from "../tokens";
import { NameLowerThird } from "./name-lower-third.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const NameLowerThirdLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} align="bottom">
      <NameLowerThird
        durationInFrames={durationInFrames}
        name="Name Lower Third"
        from={0}
        style={{ width: 534, height: 132, position: "relative" }}
      />
    </LookFrame>
  );
};

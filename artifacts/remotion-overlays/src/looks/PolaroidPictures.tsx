import { type Tokens } from "../tokens";
import { PolaroidPictures } from "./polaroid-pictures.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const PolaroidPicturesLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} scale={0.7}>
      <PolaroidPictures
        durationInFrames={durationInFrames}
        name="Polaroid Pictures"
        from={0}
        style={{ width: 1480, height: 640, position: "relative" }}
      />
    </LookFrame>
  );
};

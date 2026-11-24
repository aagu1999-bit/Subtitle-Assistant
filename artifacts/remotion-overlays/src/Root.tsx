import React from "react";
import { Composition } from "remotion";
import { OrbitCarousel } from "./OrbitCarousel";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="OrbitCarousel"
    component={OrbitCarousel}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={420}
  />
);

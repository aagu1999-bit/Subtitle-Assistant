import React from "react";
import { Composition } from "remotion";
import { DEFAULT_ORBIT_PROPS, OrbitCarousel, type OrbitProps } from "./OrbitCarousel";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="OrbitCarousel"
    component={OrbitCarousel}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={420}
    defaultProps={DEFAULT_ORBIT_PROPS}
    calculateMetadata={({ props }: { props: OrbitProps }) => ({
      durationInFrames: Math.max(24, Math.round((props.duration || 5) * 30)),
    })}
  />
);

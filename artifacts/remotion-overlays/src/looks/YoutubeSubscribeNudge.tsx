import { YouTubeSubscribeNudge } from "./youtube-subscribe-nudge.element";
import React from "react";
import { staticFile, useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const YoutubeSubscribeNudgeLook: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame align="lower" scale={0.92}>
      <YouTubeSubscribeNudge
        clickSrc={staticFile("elements/youtube-subscribe-nudge/mouse-click.wav")}
        dingSrc={staticFile("elements/youtube-subscribe-nudge/ding.wav")}
        avatarSrc={staticFile("elements/youtube-subscribe-nudge/remotion-logo.png")}
        durationInFrames={durationInFrames}
        name="YouTube Subscribe Nudge"
        from={0}
        style={{ width: 760, height: 240, position: "relative" }}
      />
    </LookFrame>
  );
};

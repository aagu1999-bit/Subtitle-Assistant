import { type Tokens } from "../tokens";
import { YouTubeCommentHighlight } from "./youtube-comment-highlight.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const YoutubeCommentHighlightLook: React.FC<Partial<Tokens>> = (tokens) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame tokens={tokens} scale={0.9}>
      <YouTubeCommentHighlight
        durationInFrames={durationInFrames}
        name="YouTube Comment Highlight"
        from={0}
        style={{ width: 1120, height: 360, position: "relative" }}
      />
    </LookFrame>
  );
};

import { WordHighlightCaptions } from "./word-highlight-captions.element";
import React from "react";
import { useVideoConfig } from "remotion";
import { LookFrame } from "./LookFrame";

export const WordHighlightCaptionsLook: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  return (
    <LookFrame align="lower">
    <WordHighlightCaptions
      captions={[{
        text: "Captions",
        startMs: 0,
        endMs: 800,
        timestampMs: 400,
        confidence: null
      }, {
        text: " can",
        startMs: 800,
        endMs: 1500,
        timestampMs: 1150,
        confidence: null
      }, {
        text: " move",
        startMs: 1500,
        endMs: 2300,
        timestampMs: 1900,
        confidence: null
      }, {
        text: " with",
        startMs: 2300,
        endMs: 3100,
        timestampMs: 2700,
        confidence: null
      }, {
        text: " every",
        startMs: 3100,
        endMs: 4000,
        timestampMs: 3550,
        confidence: null
      }, {
        text: " spoken",
        startMs: 4000,
        endMs: 5100,
        timestampMs: 4550,
        confidence: null
      }, {
        text: " word.",
        startMs: 5100,
        endMs: 6500,
        timestampMs: 5800,
        confidence: null
      }]}
      combineTokensWithinMilliseconds={800}
      width={682}
      durationInFrames={durationInFrames}
      name="Word Highlight Captions"
      from={0}
      style={{ position: "relative" }}
    />
    </LookFrame>
  );
};
